"""渲染图编排：卡片 → 生图 → 落 tmp/ → 门禁采用。

要钉的顺序是「先要卡片再生图」和「生成不等于定稿」：卡片是这张图唯一的规格，缺层的 prompt
出图等于白烧一次额度；产物落 `tmp/`，定稿位要等人按门禁。采用之后旧定稿退位保留，因为用户
过两天可能想换回上一张。

模型与生图都走假实现，验的是编排与落盘，不是模型会不会好好说话。
"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any

import pytest
from PIL import Image
from sqlalchemy.orm import Session

from atelier.agents import render
from atelier.assets import archive, characters, generations, projects
from atelier.assets.projects import ProjectRef
from atelier.db import task_events
from atelier.errors import Conflict
from atelier.providers import image_gen
from atelier.providers.base import Candidate
from tests.conftest import ScriptedChat, bind_image_model, bind_text_model
from tests.test_characters import make, spec_on_disk

CARD = """ASSET-DEMO-001 — 赤瞳 渲染图
类别：character
尺寸：2048x2048
格式：png
文件名：character_赤瞳_渲染图.png
视觉描述：一只双尾兽站在废弃电厂前。
art bible 锚点：§1 冷光金属
硬性约束：双尾数量=2
四视图背景色：#FFFFFF（白色）
prompt：standing pose, red eyes, TWO distinct tails, cinematic light, 8k
negative_prompt：background clutter, watermark
"""

THIN_CARD = """ASSET-DEMO-002 — 赤瞳 渲染图
类别：character
文件名：character_赤瞳_渲染图.png
prompt：standing pose
"""


def png(width: int = 64, height: int = 64) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


class ScriptedDraw:
    """按脚本出图的假生图驱动，签名与 `image_gen.generate` 一致。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.data = png()

    def __call__(
        self,
        candidate: Candidate,
        prompt: str,
        *,
        negative_prompt: str = "",
        width: int = image_gen.DEFAULT_SIZE,
        height: int = image_gen.DEFAULT_SIZE,
        **kwargs: Any,
    ) -> image_gen.ImageReply:
        self.calls.append(
            {
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "width": width,
                "height": height,
            }
        )
        with Image.open(BytesIO(self.data)) as image:
            actual = image.size
        return image_gen.ImageReply(
            data=self.data,
            suffix=".png",
            width=actual[0],
            height=actual[1],
            params={"model": candidate.model_id, "actual_size": f"{actual[0]}x{actual[1]}"},
            latency_ms=7,
        )


@pytest.fixture
def draw() -> ScriptedDraw:
    return ScriptedDraw()


@pytest.fixture
def chat() -> ScriptedChat:
    return ScriptedChat()


@pytest.fixture
def candidates(session: Session) -> None:
    bind_text_model(session, render.SMITH)
    bind_image_model(session, render.PAINTER, code="ark-image")


@pytest.fixture
def confirmed(project: ProjectRef, project_db: Session) -> characters.Character:
    """一个设定已经人工确认过的角色，够开工出渲染图。"""
    character = make(project_db, project)
    spec_on_disk(project, character)
    characters.confirm_spec(project_db, project, character)
    return character


def run(
    project_db: Session,
    session: Session,
    ref: ProjectRef,
    character: characters.Character,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    **kwargs: Any,
) -> render.RenderResult:
    return render.render(project_db, session, ref, character, chat=chat, generate=draw, **kwargs)


# --------------------------------------------------------------------------- #
# 卡片
# --------------------------------------------------------------------------- #


def test_设定没确认就不出卡片(
    project: ProjectRef, project_db: Session, session: Session, chat: ScriptedChat, candidates: None
) -> None:
    """卡片是设定的翻译，底本没定稿就没有可翻译的东西。"""
    character = make(project_db, project)

    with pytest.raises(Conflict, match="才能出渲染图卡片"):
        render.make_spec(project_db, session, project, character, chat=chat)


def test_卡片带上项目风格与视觉规范(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    """prompt_smith 看见了什么才是要钉住的东西。"""
    config = projects.read_config(project.dir)
    config.style.art_style = "冷光金属"
    projects.write_config(project.dir, config)
    chat.replies.append(CARD)

    spec = render.make_spec(project_db, session, project, confirmed, chat=chat)

    asked = chat.calls[-1][-1]["content"]
    assert "冷光金属" in asked
    assert "视觉规范" in asked
    assert "2048x2048" in asked
    assert spec.code == "ASSET-DEMO-001"
    assert spec.constraints == ("双尾数量=2",)
    assert spec.view_background_color == "#FFFFFF"
    assert "不要求透明背景" in asked


def test_卡片缺项先让写手自己补(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    """重跑一次卡片比重跑一次生图便宜得多。"""
    chat.replies.extend([THIN_CARD, CARD])

    spec = render.make_spec(project_db, session, project, confirmed, chat=chat)

    assert spec.gaps() == ()
    assert "尺寸" in chat.calls[-1][-1]["content"]
    events = [one.event for one in task_events.history(project_db, confirmed.id)]
    assert "asset_spec_incomplete" in events
    assert "asset_spec_drafted" in events


def test_补不齐就停下让人看(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    chat.replies.extend([THIN_CARD] * (render.MAX_SPEC_RETRIES + 1))

    with pytest.raises(Conflict, match="卡片还缺"):
        render.make_spec(project_db, session, project, confirmed, chat=chat)


def test_一张卡片都没有就报清楚(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    chat.replies.append("我觉得这个角色的设定还有点问题，先聊聊？")

    with pytest.raises(Conflict, match="没输出可解析的卡片"):
        render.make_spec(project_db, session, project, confirmed, chat=chat)


def test_改某一项只发那一项回去(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    """整张重生会顺手把用户上一轮认可的部分也改掉。"""
    chat.replies.append(CARD)

    render.make_spec(
        project_db, session, project, confirmed, chat=chat, field="光照", note="太暗了"
    )

    asked = chat.calls[-1][-1]["content"]
    assert "光照" in asked
    assert "太暗了" in asked
    assert "只改卡片里跟这一项相关的内容" in asked


def test_透明背景要求会触发补卡且不会进入生图(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    transparent = CARD.replace("cinematic light, 8k", "cinematic light, transparent background")
    chat.replies.extend([transparent, CARD])

    result = run(project_db, session, project, confirmed, chat, draw)

    assert len(chat.calls) == 2
    assert "不得要求透明背景" in chat.calls[-1][-1]["content"]
    assert "transparent background" not in draw.calls[-1]["prompt"]
    assert result.spec.view_background_color == "#FFFFFF"


# --------------------------------------------------------------------------- #
# 尺寸
# --------------------------------------------------------------------------- #


def test_尺寸按卡片优先(project: ProjectRef) -> None:
    from atelier.agents.parsing import parse_asset_specs

    (spec,) = parse_asset_specs(CARD.replace("2048x2048", "1024x1536"))

    assert render.image_size(project, spec) == (1024, 1536)


def test_卡片没说就取项目默认(project: ProjectRef) -> None:
    config = projects.read_config(project.dir)
    config.defaults.image_size = 3072
    projects.write_config(project.dir, config)

    assert render.image_size(project) == (3072, 3072)


# --------------------------------------------------------------------------- #
# 生图
# --------------------------------------------------------------------------- #


def test_图落在tmp里而不是定稿位(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    """生成即定稿等于 Agent 替人拍了板。"""
    chat.replies.append(CARD)

    result = run(project_db, session, project, confirmed, chat, draw)

    assert "/tmp/" in result.file_path
    assert result.file_path.startswith("characters/赤瞳/tmp/赤瞳_渲染图_v1_")
    assert project.absolute(result.file_path).read_bytes() == draw.data
    assert not project.absolute("characters/赤瞳/images/character_赤瞳_渲染图.png").exists()


def test_卡片里的prompt原样送出去(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    chat.replies.append(CARD)

    result = run(project_db, session, project, confirmed, chat, draw)

    sent = draw.calls[-1]
    assert sent["prompt"] == result.spec.prompt
    assert sent["negative_prompt"] == "background clutter, watermark"
    assert (sent["width"], sent["height"]) == (2048, 2048)


def test_生成后推到S2并留下台账(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    chat.replies.append(CARD)

    result = run(project_db, session, project, confirmed, chat, draw)

    assert confirmed.state == characters.RENDER_GENERATED
    assert confirmed.gate_render_confirmed_at is None
    row = generations.get(project_db, result.generation_id)
    assert row is not None
    assert row.is_final is False
    assert row.asset_spec["prompt"] == result.spec.prompt
    assert row.asset_spec["view_background_color"] == "#FFFFFF"
    assert row.asset_spec["params"]["model"]


def test_回报按约定的格式进时间线(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    chat.replies.append(CARD)

    result = run(project_db, session, project, confirmed, chat, draw)

    events = task_events.history(project_db, confirmed.id)
    reported = [one for one in events if one.event == "render_generated"]
    assert reported[-1].message.startswith("IMAGE-RESULT: OK")
    assert result.file_path in reported[-1].message


def test_连生两张各自留一份(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    """不落盘的话用户点「再来一张」时上一张就永远找不回来了。"""
    chat.replies.extend([CARD, CARD])

    first = run(project_db, session, project, confirmed, chat, draw)
    second = run(project_db, session, project, confirmed, chat, draw)

    assert first.file_path != second.file_path
    assert project.absolute(first.file_path).is_file()
    assert len(generations.candidates(project_db, target_ref=confirmed.id, stage=render.STAGE)) == 2


def test_参数快照与卡片都进meta(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    """半年后想复现这张图，靠的就是它。"""
    chat.replies.append(CARD)

    run(project_db, session, project, confirmed, chat, draw)

    meta = json.loads(characters.meta_path(project, confirmed).read_text(encoding="utf-8"))
    assert meta["render"]["asset_spec"]["code"] == "ASSET-DEMO-001"
    assert meta["render"]["params"]["model"]
    assert meta["character"]["state"] == characters.RENDER_GENERATED


# --------------------------------------------------------------------------- #
# 门禁 2
# --------------------------------------------------------------------------- #


def adopt_latest(
    project_db: Session, project: ProjectRef, character: characters.Character, **kwargs: Any
) -> Any:
    row = generations.latest(project_db, target_ref=character.id, stage=render.STAGE)
    assert row is not None
    return render.adopt(project_db, project, character, row, **kwargs)


def test_采用之后定稿位上才有图(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    chat.replies.append(CARD)
    result = run(project_db, session, project, confirmed, chat, draw)

    adopted = adopt_latest(project_db, project, confirmed, note="就这张")

    assert adopted.target_path == "characters/赤瞳/images/character_赤瞳_渲染图.png"
    assert project.absolute(adopted.target_path).read_bytes() == draw.data
    # 候选留着不动，用户过两天想换回来还能找到
    assert project.absolute(result.file_path).is_file()
    assert confirmed.state == characters.RENDER_CONFIRMED
    assert confirmed.render_path == adopted.target_path
    assert confirmed.gate_render_confirmed_at is not None


def test_换定稿时旧的退位到tmp(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    """原地覆盖一旦写坏就什么都不剩了。"""
    chat.replies.extend([CARD, CARD])
    run(project_db, session, project, confirmed, chat, draw)
    adopt_latest(project_db, project, confirmed)
    draw.data = png(96, 96)
    run(project_db, session, project, confirmed, chat, draw)
    row = generations.latest(project_db, target_ref=confirmed.id, stage=render.STAGE)
    assert row is not None

    # 已经定稿过一次，状态机不让再确认；这里只验落盘那一半
    second = archive.adopt_file(
        project,
        source_path=row.file_path,
        target_path=characters.render_target(confirmed, "character_赤瞳_渲染图.png"),
    )

    assert second.previous_path is not None
    assert "/tmp/" in second.previous_path
    assert project.absolute(second.target_path).read_bytes() == draw.data


def test_采用的台账指向定稿位(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    chat.replies.append(CARD)
    result = run(project_db, session, project, confirmed, chat, draw)

    adopt_latest(project_db, project, confirmed)

    row = generations.get(project_db, result.generation_id)
    assert row is not None
    assert row.is_final is True
    assert row.file_path == "characters/赤瞳/images/character_赤瞳_渲染图.png"


def test_没生成过就采用不了(
    project_db: Session, project: ProjectRef, confirmed: characters.Character
) -> None:
    with pytest.raises(Conflict, match="才能定稿渲染图"):
        characters.confirm_render(
            project_db, project, confirmed, render_path="characters/赤瞳/images/x.png"
        )


def test_驳回时状态停在S2(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    """驳回不是一个新阶段，是「这一步还没过」。"""
    chat.replies.append(CARD)
    run(project_db, session, project, confirmed, chat, draw)

    characters.reject_render(project_db, confirmed, note="尾巴粘在一起了")

    assert confirmed.state == characters.RENDER_GENERATED
    events = task_events.history(project_db, confirmed.id)
    rejected = [one for one in events if one.event == "gate_render_rejected"]
    assert rejected[-1].message == "尾巴粘在一起了"
    assert rejected[-1].level == "warning"


def test_驳回要写理由(project_db: Session, confirmed: characters.Character) -> None:
    with pytest.raises(Conflict, match="写清哪里不行"):
        characters.reject_render(project_db, confirmed, note="   ")


def test_别的角色的产物采用不了(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    candidates: None,
    confirmed: characters.Character,
) -> None:
    chat.replies.append(CARD)
    run(project_db, session, project, confirmed, chat, draw)
    row = generations.latest(project_db, target_ref=confirmed.id, stage=render.STAGE)
    assert row is not None
    row.target_ref = "CHAR-别人"

    with pytest.raises(Conflict, match="不是该角色的渲染图"):
        render.adopt(project_db, project, confirmed, row)
