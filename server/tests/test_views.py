"""单张四宫格四视图：参考图、生成、机器检查、台账与定稿。"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy.orm import Session

from atelier.agents import parsing, views
from atelier.assets import archive, characters, documents, generations, layout, projects
from atelier.assets.projects import ProjectRef
from atelier.db import task_events
from atelier.db.project_models import Generation
from atelier.errors import Conflict
from atelier.providers import image_gen
from atelier.providers.base import Candidate
from tests.conftest import action_reply, bind_image_model
from tests.test_characters import make, spec_on_disk

CARD = action_reply(
    "素材规格已生成。",
    reason="素材规格已完成",
    payload={
        "asset_specs": [
            {
                "code": "ASSET-DEMO-001",
                "name": "赤瞳 渲染图",
                "category": "character",
                "size": "2048x2048",
                "format": "png",
                "file_name": "character_赤瞳_渲染图.png",
                "description": "一只双尾兽站立展示完整轮廓。",
                "anchors": "§1 冷光金属",
                "constraints": ["双尾数量=2"],
                "view_background_color": "#FFFFFF（白色）",
                "prompt": (
                    "standing pose, red eyes, TWO distinct tails, fitted clothing, "
                    "cinematic light, 8k"
                ),
                "negative_prompt": "background clutter, watermark, cape, cloak",
            }
        ]
    },
)


def solid_png(
    color: tuple[int, int, int], width: int = 2048, height: int = 2048, *, cells: bool = True
) -> bytes:
    """目标纯色底；2048 方图默认在四格分别放一个主体。"""
    image = Image.new("RGB", (width, height), color)
    if cells and (width, height) == (views.VIEW_SIZE, views.VIEW_SIZE):
        for left, top, right, bottom in (
            (0, 0, 1024, 1024),
            (1024, 0, 2048, 1024),
            (0, 1024, 1024, 2048),
            (1024, 1024, 2048, 2048),
        ):
            image.paste((20, 20, 20), (left + 360, top + 180, right - 360, bottom - 180))
    else:
        image.paste(
            (20, 20, 20),
            (width // 3, height // 4, width * 2 // 3, height * 3 // 4),
        )
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def white_png(width: int = 2048, height: int = 2048) -> bytes:
    return solid_png((255, 255, 255), width, height)


def gray_png(width: int = 2048, height: int = 2048) -> bytes:
    return solid_png((200, 200, 200), width, height)


class ScriptedDraw:
    """一次调用返回一张可配置的四宫格。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.data = white_png()
        self.error: Exception | None = None

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
        self.calls.append(
            {
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "width": width,
                "height": height,
                "seed": seed,
                "references": [str(one) for one in references],
            }
        )
        if self.error is not None:
            raise self.error
        with Image.open(BytesIO(self.data)) as image:
            actual = image.size
        return image_gen.ImageReply(
            data=self.data,
            suffix=".png",
            width=actual[0],
            height=actual[1],
            params={"model": candidate.model_id, "actual_size": f"{actual[0]}x{actual[1]}"},
            latency_ms=9,
        )


@pytest.fixture
def draw() -> ScriptedDraw:
    return ScriptedDraw()


@pytest.fixture
def candidates(session: Session) -> None:
    bind_image_model(session, views.PAINTER, code="ark-image")


def stage_render(project_db: Session, ref: ProjectRef) -> characters.Character:
    """把角色摆到 S3，并造出可复用的渲染图定稿台账。"""
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


def test_姿势模版项目里那份优先(project: ProjectRef) -> None:
    assert views.pose_template(project).name == views.POSE_TEMPLATE
    local = project.dir / layout.TEMPLATES_DIR / views.POSE_TEMPLATE
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(white_png())
    assert views.pose_template(project) == local


def test_project_json指的模版不在就拒(project: ProjectRef) -> None:
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
    template, render = views.reference_images(project, staged)
    assert template.name == views.POSE_TEMPLATE
    assert staged.render_path
    assert render == project.absolute(staged.render_path)


def test_prompt固定四格背景附属结构与无披风(
    project_db: Session, staged: characters.Character
) -> None:
    card = views.base_card(project_db, staged)
    prompt = views.build_prompt(card, staged)
    assert prompt.startswith(card["prompt"])
    assert "top-left is front view" in prompt
    assert "bottom-right is left-side 30-degree view" in prompt
    assert "#FFFFFF" in prompt
    assert "尾巴数量 = 2" in prompt
    assert "not merged" in prompt
    assert "must not wear a cape" in prompt


def test_negative补齐布局与无披风禁止词(project_db: Session, staged: characters.Character) -> None:
    negative = views.build_negative(views.base_card(project_db, staged))
    assert all(one in negative for one in views.NEGATIVE_MUST)
    assert [part.strip() for part in negative.split(",")].count("watermark") == 1


def test_旧卡片缺少背景色时要求重做(project_db: Session, staged: characters.Character) -> None:
    row = generations.final(project_db, target_ref=staged.id, stage=generations.RENDER)
    assert row is not None
    row.asset_spec.pop("view_background_color")
    with pytest.raises(Conflict, match="重新生成并定稿"):
        views.base_card(project_db, staged)


def test_尺寸始终固定2048(
    project: ProjectRef, project_db: Session, staged: characters.Character
) -> None:
    card = views.base_card(project_db, staged)
    assert views.image_size(project, card) == (2048, 2048)
    assert views.image_size(project, {**card, "size": "512x512"}) == (2048, 2048)


def test_一次生成一张且带两张参考图(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    result = run(project_db, session, project, staged, draw, seed=42)
    assert len(draw.calls) == 1
    call = draw.calls[0]
    assert (call["width"], call["height"]) == (2048, 2048)
    assert len(call["references"]) == 2
    assert call["references"][0].endswith(views.POSE_TEMPLATE)
    assert call["seed"] == 42
    assert [one.variant for one in result.images] == [views.SHEET_CODE]
    assert result.ok
    assert result.state == characters.VIEWS_GENERATED


def test_图落tmp并按sheet登台账和meta(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    result = run(project_db, session, project, staged, draw)
    image = result.images[0]
    assert "/tmp/" in image.file_path
    assert project.absolute(image.file_path).is_file()
    rows = generations.candidates(project_db, target_ref=staged.id, stage=views.STAGE)
    assert [row.variant for row in rows] == [views.SHEET_CODE]
    assert rows[0].asset_spec["view_layout"] == views.VIEW_LAYOUT
    assert rows[0].asset_spec["view_positions"] == views.VIEW_POSITIONS
    meta = json.loads(characters.meta_path(project, staged).read_text(encoding="utf-8"))
    assert len(meta["views"]["images"]) == 1
    assert meta["views"]["images"][0]["variant"] == views.SHEET_CODE
    assert not project.absolute(characters.document_targets(staged)["views_prompt"]).exists()


def test_机器不通过就不推S4但保留候选(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    draw.data = gray_png()
    result = run(project_db, session, project, staged, draw)
    assert not result.ok
    assert result.state == characters.RENDER_CONFIRMED
    assert any("目标纯色 #FFFFFF" in one for one in result.images[0].problems)
    assert project.absolute(result.images[0].file_path).is_file()
    flagged = [
        one for one in task_events.history(project_db, staged.id) if one.event == "view_suspect"
    ]
    assert flagged[-1].level == "warning"


def test_非白动态背景贯穿生成与校验(
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
    assert "#33CC99" in draw.calls[0]["prompt"]
    assert result.images[0].params["background"]["target_color"] == "#33CC99"
    assert len(result.images[0].params["background"]["regions"]) == 4


def test_生成失败以sheet返回且不推进(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    draw.error = RuntimeError("整张画不出来")
    result = run(project_db, session, project, staged, draw)
    assert result.images == ()
    assert result.failures[0].variant == views.SHEET_CODE
    assert "画不出来" in result.failures[0].reason
    assert result.state == characters.RENDER_CONFIRMED


def test_局部重生参数被拒绝(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    with pytest.raises(Conflict, match="只能整张"):
        run(project_db, session, project, staged, draw, variants=(views.BY_CODE["back"],))
    assert draw.calls == []


def adopt_latest(
    project_db: Session,
    ref: ProjectRef,
    character: characters.Character,
    **kwargs: Any,
) -> dict[str, archive.ArchiveResult]:
    row = views.latest_sheet(project_db, character)
    assert row is not None
    return views.adopt(project_db, ref, character, {views.SHEET_CODE: row}, **kwargs)


def test_定稿一张四宫格并推S5(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    run(project_db, session, project, staged, draw, seed=42)
    results = adopt_latest(project_db, project, staged, note="可以建模")
    assert staged.state == characters.VIEWS_CONFIRMED
    paths = characters.view_paths(staged)
    assert set(paths) == {views.SHEET_CODE}
    assert paths[views.SHEET_CODE] == "characters/赤瞳/images/character_赤瞳_四视图.png"
    assert project.absolute(paths[views.SHEET_CODE]).is_file()
    assert results[views.SHEET_CODE].target_path == paths[views.SHEET_CODE]
    finals = [
        row
        for row in generations.candidates(project_db, target_ref=staged.id, stage=views.STAGE)
        if row.is_final
    ]
    assert len(finals) == 1
    prompt_path = project.absolute(characters.document_targets(staged)["views_prompt"])
    prompt_doc = prompt_path.read_text(encoding="utf-8")
    assert prompt_path.name == layout.VIEWS_PROMPT_MD
    assert finals[0].id in prompt_doc
    assert paths[views.SHEET_CODE] in prompt_doc
    assert "top-left is front view" in prompt_doc
    assert '"view_layout": "grid-2x2"' in prompt_doc
    assert '"top-left": "front"' in prompt_doc
    assert "#FFFFFF" in prompt_doc
    assert "Seed：42" in prompt_doc


def test_四视图提示词文档失败时不推进定稿(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run(project_db, session, project, staged, draw)
    row = views.latest_sheet(project_db, staged)
    assert row is not None

    def fail(*args: Any, **kwargs: Any) -> str:
        raise OSError("docs 不可写")

    monkeypatch.setattr(documents, "write_prompt_document", fail)
    with pytest.raises(OSError, match="docs 不可写"):
        views.adopt(project_db, project, staged, {views.SHEET_CODE: row})

    assert row.is_final is False
    assert staged.state == characters.VIEWS_GENERATED
    assert characters.view_paths(staged) == {}
    assert not project.absolute(characters.views_target(staged, "四视图")).exists()


def test_旧分图不能混入新定稿(
    project: ProjectRef, project_db: Session, staged: characters.Character
) -> None:
    fake = Generation(target_ref=staged.id, stage=views.STAGE, variant="front", file_path="x.png")
    with pytest.raises(Conflict, match="旧分图只能查看"):
        views.adopt(project_db, project, staged, {"front": fake})


def test_机器不通过的四宫格不能定稿(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    candidates: None,
    staged: characters.Character,
) -> None:
    draw.data = gray_png()
    run(project_db, session, project, staged, draw)
    with pytest.raises(Conflict, match="机器校验未通过"):
        adopt_latest(project_db, project, staged)


def test_定稿路径仍兼容指定扩展名(staged: characters.Character) -> None:
    target = characters.views_target(staged, "四视图", ".jpg")
    assert target == "characters/赤瞳/images/character_赤瞳_四视图.jpg"
    assert Path(target).suffix == ".jpg"
