"""四视图编排：两张参考图 → 并发四张 → 机器量白底 → 人选输入定稿。

要钉住的是这一步「没有想象空间」这件事：参考图缺一不做、prompt 就是渲染图那张卡片、白底靠
negative 硬压、背面强制注入附属结构的数量与分离状态。四张里少一张就不推 S4，某一张失败也不
准拖累其他三张。

生图走假驱动，验的是编排、落盘与台账，不是模型画得好不好。
"""

from __future__ import annotations

import json
import threading
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy.orm import Session

from atelier.agents import parsing, views
from atelier.assets import archive, characters, generations, layout, projects
from atelier.assets.projects import ProjectRef
from atelier.db import task_events
from atelier.db.project_models import Generation
from atelier.errors import Conflict
from atelier.providers import image_gen
from atelier.providers.base import Candidate
from tests.conftest import bind_image_model
from tests.test_characters import make, spec_on_disk

CARD = """ASSET-DEMO-001 — 赤瞳 渲染图
类别：character
尺寸：512x512
格式：png
文件名：character_赤瞳_渲染图.png
视觉描述：一只双尾兽站在废弃电厂前。
art bible 锚点：§1 冷光金属
硬性约束：双尾数量=2
四视图背景色：#FFFFFF（白色）
prompt：standing pose, red eyes, TWO distinct tails, cinematic light, 8k
negative_prompt：background clutter, watermark
"""


def solid_png(color: tuple[int, int, int], width: int = 512, height: int = 512) -> bytes:
    """指定纯色底 + 居中主体。"""
    image = Image.new("RGB", (width, height), color)
    block = Image.new("RGB", (width // 3, height // 3), (20, 20, 20))
    image.paste(block, (width // 3, height // 3))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def white_png(width: int = 512, height: int = 512) -> bytes:
    return solid_png((255, 255, 255), width, height)


def gray_png(width: int = 512, height: int = 512) -> bytes:
    """带底色的图，量出来就是「背景不是纯白」。"""
    buffer = BytesIO()
    Image.new("RGB", (width, height), (200, 200, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def variant_of(prompt: str) -> str:
    for one in views.VARIANTS:
        if one.clause in prompt:
            return one.code
    return ""


class ScriptedDraw:
    """按变体出图的假生图驱动。四个线程一起进来，所以记账要上锁。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []
        self.data = white_png()
        self.per_variant: dict[str, bytes] = {}
        self.fail: set[str] = set()

    def __call__(
        self,
        candidate: Candidate,
        prompt: str,
        *,
        negative_prompt: str = "",
        width: int = image_gen.DEFAULT_SIZE,
        height: int = image_gen.DEFAULT_SIZE,
        seed: int | None = None,
        references: Any = (),
        **kwargs: Any,
    ) -> image_gen.ImageReply:
        code = variant_of(prompt)
        with self._lock:
            self.calls.append(
                {
                    "variant": code,
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "width": width,
                    "height": height,
                    "seed": seed,
                    "references": [str(one) for one in references],
                }
            )
        if code in self.fail:
            raise RuntimeError(f"{code} 这张画不出来")
        data = self.per_variant.get(code, self.data)
        with Image.open(BytesIO(data)) as image:
            actual = image.size
        return image_gen.ImageReply(
            data=data,
            suffix=".png",
            width=actual[0],
            height=actual[1],
            params={"model": candidate.model_id, "actual_size": f"{actual[0]}x{actual[1]}"},
            latency_ms=9,
        )

    def by_variant(self, code: str) -> dict[str, Any]:
        picked = [one for one in self.calls if one["variant"] == code]
        assert picked, f"{code} 那张没发出去"
        return picked[-1]


@pytest.fixture
def draw() -> ScriptedDraw:
    return ScriptedDraw()


@pytest.fixture
def candidates(session: Session) -> None:
    bind_image_model(session, views.PAINTER, code="ark-image")


def stage_render(project_db: Session, ref: ProjectRef) -> characters.Character:
    """把一个角色摆到「渲染图已定稿」（S3），够开工出四视图。

    直接造台账与定稿位，不跑一遍渲染那一步：这里验的是四视图，前一步的编排在
    `test_render.py` 里已经钉过。
    """
    character = make(project_db, ref)
    spec_on_disk(ref, character)
    characters.confirm_spec(project_db, ref, character)
    character.hard_constraints = {"items": [{"item": "尾巴数量", "value": "2"}]}

    relative = characters.render_target(character, "character_赤瞳_渲染图.png")
    path = ref.absolute(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(white_png())
    row = generations.record(
        project_db,
        target_ref=character.id,
        stage=generations.RENDER,
        file_path=relative,
        file_hash=archive.file_hash(path),
        task_id=character.id,
        asset_spec=parsing.parse_asset_specs(CARD)[0].as_dict(),
    )
    generations.mark_final(project_db, row, file_path=relative, file_hash=row.file_hash)
    characters.advance(project_db, ref, character, characters.RENDER_GENERATED)
    characters.confirm_render(project_db, ref, character, render_path=relative)
    return character


@pytest.fixture
def staged(project: ProjectRef, project_db: Session) -> characters.Character:
    return stage_render(project_db, project)


def run(
    project_db: Session,
    session: Session,
    ref: ProjectRef,
    character: characters.Character,
    draw: ScriptedDraw,
    **kwargs: Any,
) -> views.ViewSet:
    return views.generate_views(project_db, session, ref, character, generate=draw, **kwargs)


# --------------------------------------------------------------------------- #
# 参考图
# --------------------------------------------------------------------------- #


def test_姿势模版项目里那份优先(
    project: ProjectRef, project_db: Session, staged: characters.Character
) -> None:
    """项目可以有自己的排版规范，没有才用仓库里那份通用的。"""
    assert views.pose_template(project).name == views.POSE_TEMPLATE

    local = project.dir / layout.TEMPLATES_DIR / views.POSE_TEMPLATE
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(white_png())

    assert views.pose_template(project) == local


def test_project_json指的模版不在就直接拒(project: ProjectRef) -> None:
    """退化成纯文字生成的话，四张图的角度与画幅会各说各话。"""
    config = projects.read_config(project.dir)
    config.pose_template = "templates/没有这份.jpg"
    projects.write_config(project.dir, config)

    with pytest.raises(Conflict, match="不在磁盘上"):
        views.pose_template(project)


def test_渲染图没定稿就不出四视图(project: ProjectRef, project_db: Session) -> None:
    character = make(project_db, project)
    spec_on_disk(project, character)
    characters.confirm_spec(project_db, project, character)

    with pytest.raises(Conflict, match="才能生成四视图"):
        views.reference_images(project, character)


def test_定稿渲染图不在磁盘上就拒(project: ProjectRef, staged: characters.Character) -> None:
    assert staged.render_path
    project.absolute(staged.render_path).unlink()

    with pytest.raises(Conflict, match="重新定稿"):
        views.reference_images(project, staged)


def test_两张参考图先模版后渲染图(project: ProjectRef, staged: characters.Character) -> None:
    """顺序也是约定的一部分：倒过来传会让模版的画风盖过角色。"""
    template, render = views.reference_images(project, staged)

    assert template.name == views.POSE_TEMPLATE
    assert staged.render_path
    assert render == project.absolute(staged.render_path)


# --------------------------------------------------------------------------- #
# prompt 组装
# --------------------------------------------------------------------------- #


def test_卡片就是渲染图那一版(project_db: Session, staged: characters.Character) -> None:
    card = views.base_card(project_db, staged)

    assert card["code"] == "ASSET-DEMO-001"
    assert "TWO distinct tails" in card["prompt"]


def test_渲染图没定稿卡片就没有规格可依(project_db: Session, staged: characters.Character) -> None:
    row = generations.final(project_db, target_ref=staged.id, stage=generations.RENDER)
    assert row is not None
    row.asset_spec = {"code": "ASSET-DEMO-001", "prompt": "  "}

    with pytest.raises(Conflict, match="没有规格可依"):
        views.base_card(project_db, staged)


def test_prompt追加视角句与动态纯色背景(project_db: Session, staged: characters.Character) -> None:
    card = views.base_card(project_db, staged)

    prompt = views.build_prompt(card, views.BY_CODE["right"], staged)

    assert prompt.startswith(card["prompt"])
    assert views.BY_CODE["right"].clause in prompt
    assert views.BACKGROUND_CLAUSE.format(color="#FFFFFF") in prompt


def test_旧卡片缺少背景色时要求重新生成(project_db: Session, staged: characters.Character) -> None:
    row = generations.final(project_db, target_ref=staged.id, stage=generations.RENDER)
    assert row is not None
    row.asset_spec.pop("view_background_color")

    with pytest.raises(Conflict, match="重新生成并定稿渲染规格卡片"):
        views.base_card(project_db, staged)


def test_四视图使用卡片选择的同一背景色(project_db: Session, staged: characters.Character) -> None:
    row = generations.final(project_db, target_ref=staged.id, stage=generations.RENDER)
    assert row is not None
    row.asset_spec["view_background_color"] = "#33CC99"
    card = views.base_card(project_db, staged)

    prompts = [views.build_prompt(card, variant, staged) for variant in views.VARIANTS]

    assert all("Solid #33CC99 background" in prompt for prompt in prompts)
    assert all("Pure white background" not in prompt for prompt in prompts)


def test_背面图注入附属结构的数量与分离状态(
    project_db: Session, staged: characters.Character
) -> None:
    """糊成一团要到建模出网格才看得出来，那时候重来的代价是整条流水线。"""
    card = views.base_card(project_db, staged)

    back = views.build_prompt(card, views.BY_CODE["back"], staged)
    front = views.build_prompt(card, views.BY_CODE["front"], staged)

    assert "尾巴数量 = 2" in back
    assert "not merged" in back
    assert "not merged" not in front


def test_negative补齐必备词且不重复(project_db: Session, staged: characters.Character) -> None:
    """白底靠 negative 硬压，卡片自己写了的就不重复塞。"""
    card = views.base_card(project_db, staged)

    negative = views.build_negative(card)

    assert all(one in negative for one in views.NEGATIVE_MUST)
    terms = [part.strip() for part in negative.split(",")]
    assert "background" not in terms
    assert "gray background" not in terms
    assert terms.count("watermark") == 1


def test_尺寸跟渲染图卡片一致(
    project: ProjectRef, project_db: Session, staged: characters.Character
) -> None:
    """建模吃的是一整组图，其中一张画幅不同就等于说这个面的比例不一样。"""
    card = views.base_card(project_db, staged)

    assert views.image_size(project, card) == (512, 512)
    assert (
        views.image_size(project, {**card, "size": "0x0"})
        == (projects.read_config(project.dir).defaults.image_size,) * 2
    )


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #


def test_四个视角都发出去且都带两张参考图(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    result = run(project_db, session, project, staged, draw)

    assert len(draw.calls) == 4
    assert {one["variant"] for one in draw.calls} == set(views.BY_CODE)
    for call in draw.calls:
        assert len(call["references"]) == 2
        assert call["references"][0].endswith(views.POSE_TEMPLATE)
        assert staged.render_path
        assert call["references"][1].endswith("character_赤瞳_渲染图.png")
        assert (call["width"], call["height"]) == (512, 512)
    assert result.ok
    assert result.references[0].endswith(views.POSE_TEMPLATE)
    assert result.references[1] == staged.render_path


def test_图落tmp并按变体登台账(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    """生成即落盘，但落的是候选位——定稿位要等人选完输入。"""
    result = run(project_db, session, project, staged, draw)

    rows = generations.candidates(project_db, target_ref=staged.id, stage=views.STAGE)
    assert {row.variant for row in rows} == set(views.BY_CODE)
    assert all(not row.is_final for row in rows)
    for one in result.images:
        assert "/tmp/" in one.file_path
        assert project.absolute(one.file_path).is_file()
        assert one.params["references"]
        assert one.params["variant"] == one.variant


def test_四张齐了才推S4(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    """S4 的含义是「四个面都有图可看」，提前推等于让门禁凭一个不成立的前提亮起来。"""
    half = run(
        project_db,
        session,
        project,
        staged,
        draw,
        variants=(views.BY_CODE["front"], views.BY_CODE["back"]),
    )

    assert half.state == characters.RENDER_CONFIRMED

    full = run(
        project_db,
        session,
        project,
        staged,
        draw,
        variants=(views.BY_CODE["left"], views.BY_CODE["right"]),
    )

    assert full.state == characters.VIEWS_GENERATED
    advanced = [
        one
        for one in task_events.history(project_db, staged.id)
        if one.event == "state_advanced"
        and characters.label(characters.VIEWS_GENERATED) in one.message
    ]
    assert len(advanced) == 1


def test_背景不纯只记警告不丢图(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    """判定归评审与人工，这里只把事实摆出来。"""
    draw.per_variant["back"] = gray_png()

    result = run(project_db, session, project, staged, draw)

    assert not result.ok
    suspect = [one for one in result.images if one.variant == "back"][0]
    assert any("目标纯色 #FFFFFF" in one for one in suspect.problems)
    assert project.absolute(suspect.file_path).is_file()
    events = task_events.history(project_db, staged.id)
    flagged = [one for one in events if one.event == "view_suspect"]
    assert flagged[-1].level == "warning"
    assert len(generations.candidates(project_db, target_ref=staged.id, stage=views.STAGE)) == 4


def test_非白纯色背景贯穿生成和校验(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    row = generations.final(project_db, target_ref=staged.id, stage=generations.RENDER)
    assert row is not None
    row.asset_spec["view_background_color"] = "#33CC99"
    draw.data = solid_png((51, 204, 153))

    result = run(project_db, session, project, staged, draw)

    assert result.ok
    assert all("Solid #33CC99 background" in call["prompt"] for call in draw.calls)
    assert all(image.params["background"]["target_color"] == "#33CC99" for image in result.images)
    assert all(image.params["background"]["edge_match"] == 1.0 for image in result.images)


def test_一个视角失败不拖累其他三个(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    """用户重生那一个变体就行，另外三张不该白烧一次额度。"""
    draw.fail = {"left"}

    result = run(project_db, session, project, staged, draw)

    assert len(result.images) == 3
    assert [one.variant for one in result.failures] == ["left"]
    assert result.state == characters.RENDER_CONFIRMED
    failed = [
        one for one in task_events.history(project_db, staged.id) if one.event == "view_failed"
    ]
    assert failed[-1].level == "error"
    assert "画不出来" in failed[-1].message


def test_尺寸不齐时给一句说明(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    draw.per_variant["front"] = white_png(256, 256)

    result = run(project_db, session, project, staged, draw)

    assert result.size_complaint
    assert "256x256" in result.size_complaint


def test_参数快照与参考图进meta(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    """半年后想复现这四张图，靠的就是它。"""
    run(project_db, session, project, staged, draw, seed=42)

    meta = json.loads(characters.meta_path(project, staged).read_text(encoding="utf-8"))
    snapshot = meta["views"]
    assert snapshot["asset_spec"]["code"] == "ASSET-DEMO-001"
    assert len(snapshot["references"]) == 2
    assert len(snapshot["images"]) == 4
    assert snapshot["images"][0]["params"]["model"]
    assert meta["character"]["state"] == characters.VIEWS_GENERATED
    assert draw.by_variant("front")["seed"] == 42


def test_只重生一个视角不动其他三张(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    """评审驳回往往只有背面不合格，四张全重来既费额度又会换掉已经认可的三张。"""
    first = run(project_db, session, project, staged, draw)
    kept = {one.variant: one.file_path for one in first.images}

    again = run(project_db, session, project, staged, draw, variants=(views.BY_CODE["back"],))

    assert [one.variant for one in again.images] == ["back"]
    latest = views.latest_by_variant(project_db, staged)
    assert latest["back"].file_path != kept["back"]
    assert latest["front"].file_path == kept["front"]


# --------------------------------------------------------------------------- #
# 定稿归档
# --------------------------------------------------------------------------- #


def adopt_latest(
    project_db: Session,
    ref: ProjectRef,
    character: characters.Character,
    **kwargs: Any,
) -> dict[str, archive.ArchiveResult]:
    return views.adopt(
        project_db, ref, character, views.latest_by_variant(project_db, character), **kwargs
    )


def test_四个角度不齐不给定稿(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    """缺一张就会让建模拿三张去猜第四个面，猜错要到绑骨之后才看得出来。"""
    run(project_db, session, project, staged, draw)
    chosen = views.latest_by_variant(project_db, staged)
    chosen.pop("back")

    with pytest.raises(Conflict, match="还差 背面"):
        views.adopt(project_db, project, staged, chosen)


def test_定稿把四张搬进images并推S5(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    run(project_db, session, project, staged, draw)

    results = adopt_latest(project_db, project, staged, note="这一组可以进建模")

    assert staged.state == characters.VIEWS_CONFIRMED
    paths = characters.view_paths(staged)
    assert set(paths) == set(views.BY_CODE)
    for variant in views.VARIANTS:
        relative = paths[variant.code]
        assert relative == f"characters/赤瞳/images/character_赤瞳_{variant.stem}.png"
        assert project.absolute(relative).is_file()
        assert results[variant.code].target_path == relative
    finals = [
        row
        for row in generations.candidates(project_db, target_ref=staged.id, stage=views.STAGE)
        if row.is_final
    ]
    assert len(finals) == 4
    assert all("/tmp/" not in row.file_path for row in finals)


def test_定稿的四张也进meta(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    run(project_db, session, project, staged, draw)

    adopt_latest(project_db, project, staged)

    meta = json.loads(characters.meta_path(project, staged).read_text(encoding="utf-8"))
    assert len(meta["character"]["views"]) == 4
    assert meta["character"]["state"] == characters.VIEWS_CONFIRMED


def test_别的角色的产物定稿不了(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    run(project_db, session, project, staged, draw)
    chosen: dict[str, Generation] = views.latest_by_variant(project_db, staged)
    chosen["left"].target_ref = "CHAR-别人"

    with pytest.raises(Conflict, match="不是该角色的这个视角"):
        views.adopt(project_db, project, staged, chosen)


def test_定稿之后不给再定一次(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    run(project_db, session, project, staged, draw)
    adopt_latest(project_db, project, staged)

    with pytest.raises(Conflict, match="已经定稿过了"):
        characters.confirm_views(project_db, project, staged, paths=characters.view_paths(staged))


def test_没生成过就定不了稿(
    project: ProjectRef, project_db: Session, staged: characters.Character
) -> None:
    with pytest.raises(Conflict, match="才能定稿四视图"):
        characters.confirm_views(
            project_db, project, staged, paths={"front": "characters/赤瞳/images/x.png"}
        )


def test_视图不在磁盘上就不给定稿(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    run(project_db, session, project, staged, draw)
    paths = {one.code: f"characters/赤瞳/images/丢了_{one.code}.png" for one in views.VARIANTS}

    with pytest.raises(Conflict, match="不在磁盘上"):
        characters.confirm_views(project_db, project, staged, paths=paths)


def test_没指定视角就不生成(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    with pytest.raises(Conflict, match="没有指定要生成哪个视角"):
        run(project_db, session, project, staged, draw, variants=())


def test_定稿位的文件名带类别与视角(staged: characters.Character) -> None:
    """下游引用的是「这个角色的正面图」，名字里不带版本也不带时间戳。"""
    target = characters.views_target(staged, "正面")

    assert target == "characters/赤瞳/images/character_赤瞳_正面.png"
    assert Path(target).suffix == ".png"
