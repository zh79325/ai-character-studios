"""四视图评审：`vision_reviewer` 看图裁决 + 按 `review_mode` 分粒度 + REJECT 自动重生。

要钉住三件事：模型看见了什么（图与机器读数都得在请求里）、粒度跟项目配置走（`full` 一张一
次、`lean` 一批一次、`solo` 不审）、REJECT 之后只重生被点名的那几张且重生有上限。

模型与生图都走假实现，验的是编排与裁决处理，不是模型看得准不准。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.orm import Session

from atelier.agents import views, vision
from atelier.agents.parsing import VerdictError
from atelier.assets import characters, projects
from atelier.assets.projects import ProjectRef
from atelier.db import task_events
from atelier.errors import Conflict
from tests.conftest import ScriptedChat, bind_image_model, bind_text_model
from tests.test_characters import make, spec_on_disk
from tests.test_views import ScriptedDraw, gray_png, stage_render

APPROVE = """VIEW-CHECK: APPROVE

### 硬性约束逐条
- 尾巴数量 = 2 → 实际：两条分开的尾巴 → 符合

### 检查清单
- 背景纯净度：纯白，无渐变
- 附属结构数量：2，符合
- 附属结构分离度：清晰分开
- 角色一致性：与渲染图一致
- 视角准确性：四个角度都对

### 修正建议
- 无
"""

CONCERNS = """VIEW-CHECK: CONCERNS

### 检查清单
- 视角准确性：侧视角度略偏，约 35°

### 修正建议
- 侧面那两张把 30 degrees 写得更硬一点
"""

REJECT_BACK = """VIEW-CHECK: REJECT

### 硬性约束逐条
- 尾巴数量 = 2 → 实际：粘成一条 → 不符合

### 检查清单
- 附属结构分离度：粘连

### 修正建议
- 背面那张往 prompt 加 two clearly separated tails
"""

REJECT_VAGUE = """VIEW-CHECK: REJECT

### 检查清单
- 背景纯净度：有渐变

### 修正建议
- 往 prompt 里加 pure white background, no gradient
"""

BABBLE = """这四张图我看了一下，整体感觉还不错。
VIEW-CHECK: APPROVE
"""


@pytest.fixture
def draw() -> ScriptedDraw:
    return ScriptedDraw()


@pytest.fixture
def chat() -> ScriptedChat:
    return ScriptedChat()


@pytest.fixture
def bound(session: Session) -> None:
    bind_image_model(session, views.PAINTER, code="ark-image")
    bind_text_model(session, vision.REVIEWER)


@pytest.fixture
def generated(
    project: ProjectRef, project_db: Session, session: Session, draw: ScriptedDraw, bound: None
) -> characters.Character:
    """四视图已经生成（S4）的角色，够开工评审。"""
    character = stage_render(project_db, project)
    views.generate_views(project_db, session, project, character, generate=draw)
    return character


def parts_of(call: list[dict[str, Any]], kind: str) -> list[Any]:
    content = call[-1]["content"]
    assert isinstance(content, list), "评审这一条消息得是带图的分段形态"
    return [one for one in content if one["type"] == kind]


def asked(chat: ScriptedChat, index: int = -1) -> str:
    return "".join(one["text"] for one in parts_of(chat.calls[index], "text"))


def images_in(chat: ScriptedChat, index: int = -1) -> list[str]:
    return [one["image_url"]["url"] for one in parts_of(chat.calls[index], "image_url")]


def set_mode(ref: ProjectRef, mode: str) -> None:
    config = projects.read_config(ref.dir)
    config.review_mode = mode  # type: ignore[assignment]
    projects.write_config(ref.dir, config)


# --------------------------------------------------------------------------- #
# 送审的那几张
# --------------------------------------------------------------------------- #


def test_没生成四视图就不给评审(
    project: ProjectRef, project_db: Session, session: Session, chat: ScriptedChat
) -> None:
    character = make(project_db, project)
    spec_on_disk(project, character)

    with pytest.raises(Conflict, match="才能评审四视图"):
        vision.review(project_db, session, project, character, chat=chat)


def test_图不在磁盘上就先重生再评审(
    project: ProjectRef,
    project_db: Session,
    generated: characters.Character,
) -> None:
    picked = vision.shots(project_db, project, generated)
    project.absolute(picked[0].file_path).unlink()

    with pytest.raises(Conflict, match="重生一张再评审"):
        vision.shots(project_db, project, generated)


def test_送审的是每个视角最近那一张(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    generated: characters.Character,
) -> None:
    """用户可能已经重生过某一张，拿旧的送审等于评审一张他早就换掉的图。"""
    first = {one.variant: one.file_path for one in vision.shots(project_db, project, generated)}
    views.generate_views(
        project_db,
        session,
        project,
        generated,
        variants=(views.BY_CODE["back"],),
        generate=draw,
    )

    again = {one.variant: one.file_path for one in vision.shots(project_db, project, generated)}

    assert [one.variant for one in vision.shots(project_db, project, generated)] == [
        one.code for one in views.VARIANTS
    ]
    assert again["back"] != first["back"]
    assert again["front"] == first["front"]


# --------------------------------------------------------------------------- #
# 模型看见了什么
# --------------------------------------------------------------------------- #


def test_四张图与机器读数都进请求(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    """背景够不够白是像素统计题，交给机器；模型判它擅长的那四项。"""
    chat.replies.append(APPROVE)

    vision.review(project_db, session, project, generated, chat=chat)

    assert len(chat.calls) == 1
    assert len(images_in(chat)) == 4
    assert all(one.startswith("data:image/png;base64,") for one in images_in(chat))
    request = asked(chat)
    assert "尾巴数量 = 2" in request
    assert "边缘匹配率" in request
    assert "目标背景 #FFFFFF" in request
    for variant in views.VARIANTS:
        assert variant.label in request


def test_机器判定的问题写给模型看(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    bound: None,
) -> None:
    character = stage_render(project_db, project)
    draw.per_variant["back"] = gray_png()
    views.generate_views(project_db, session, project, character, generate=draw)
    chat.replies.append(REJECT_BACK)

    vision.review(project_db, session, project, character, chat=chat)

    assert "机器判定问题" in asked(chat)
    assert "目标纯色 #FFFFFF" in asked(chat)


def test_设定原文也挂进上下文(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    """「角色一致性」这一项要对着设定判，只给图的话四张一起跑偏时它们反而是自洽的。"""
    chat.replies.append(APPROVE)

    vision.review(project_db, session, project, generated, chat=chat)

    assert "双尾、红瞳" in chat.calls[-1][0]["content"]


# --------------------------------------------------------------------------- #
# 粒度
# --------------------------------------------------------------------------- #


def test_lean是整批一次调用(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    chat.replies.append(APPROVE)

    result = vision.review(project_db, session, project, generated, chat=chat)

    assert result.mode == vision.LEAN
    assert len(result.verdicts) == 1
    assert result.verdicts[0].variants == tuple(one.code for one in views.VARIANTS)
    assert result.approved


def test_full是每张一次调用(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    set_mode(project, vision.FULL)
    chat.replies.extend([APPROVE] * 4)

    result = vision.review(project_db, session, project, generated, chat=chat)

    assert len(chat.calls) == 4
    assert all(len(images_in(chat, index)) == 1 for index in range(4))
    assert [one.variants for one in result.verdicts] == [(one.code,) for one in views.VARIANTS]


def test_solo不调用评审(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    """用户自己看，平台不拦也不烧额度。"""
    set_mode(project, vision.SOLO)

    result = vision.review(project_db, session, project, generated, chat=chat)

    assert result.skipped
    assert result.verdicts == ()
    assert chat.calls == []
    events = [one.event for one in task_events.history(project_db, generated.id)]
    assert "views_review_skipped" in events


def test_一批里取最严那一档(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    """四个面只要有一个不能用，这一组就不能进建模。"""
    set_mode(project, vision.FULL)
    chat.replies.extend([APPROVE, CONCERNS, REJECT_BACK, APPROVE])

    result = vision.review(project_db, session, project, generated, chat=chat)

    assert result.decision == "REJECT"
    assert not result.approved


# --------------------------------------------------------------------------- #
# 裁决落库
# --------------------------------------------------------------------------- #


def test_裁决全文进事件(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    """摘成一句「CONCERNS 1 处」等于把证据丢了。"""
    chat.replies.append(CONCERNS)

    vision.review(project_db, session, project, generated, chat=chat)

    reviewed = [
        one
        for one in task_events.history(project_db, generated.id)
        if one.event == "views_reviewed"
    ]
    assert reviewed[-1].message == CONCERNS.strip()
    assert reviewed[-1].level == "warning"
    assert reviewed[-1].payload["decision"] == "CONCERNS"
    assert len(reviewed[-1].payload["generation_ids"]) == 4


def test_首行不是裁决就当格式事故(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    """默认成 REJECT 会把一次格式事故变成一次没有理由的驳回。"""
    chat.replies.append(BABBLE)

    with pytest.raises(VerdictError):
        vision.review(project_db, session, project, generated, chat=chat)

    events = task_events.history(project_db, generated.id)
    unparsable = [one for one in events if one.event == "views_review_unparsable"]
    assert unparsable[-1].level == "error"


def test_评审结论进meta且不抹掉参数快照(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    chat.replies.append(APPROVE)

    vision.review(project_db, session, project, generated, chat=chat)

    meta = json.loads(characters.meta_path(project, generated).read_text(encoding="utf-8"))
    assert meta["views"]["review"]["decision"] == "APPROVE"
    assert meta["views"]["review"]["mode"] == vision.LEAN
    assert len(meta["views"]["images"]) == 4
    assert meta["views"]["asset_spec"]["code"] == "ASSET-DEMO-001"


# --------------------------------------------------------------------------- #
# 自动重生
# --------------------------------------------------------------------------- #


def test_点名了就只重生那一张(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    generated: characters.Character,
) -> None:
    """四张全重来既多烧三次额度，又会把用户已经认可的三张换掉。"""
    chat.replies.extend([REJECT_BACK, APPROVE])
    before = {one.variant: one.file_path for one in vision.shots(project_db, project, generated)}

    result = vision.review(project_db, session, project, generated, chat=chat, generate=draw)

    assert result.regenerated == 1
    assert result.approved
    assert [one["variant"] for one in draw.calls[4:]] == ["back"]
    after = {one.variant: one.file_path for one in vision.shots(project_db, project, generated)}
    assert after["front"] == before["front"]
    assert after["back"] != before["back"]


def test_点不出名就整批重生(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    generated: characters.Character,
) -> None:
    """宁可多花额度，也不能因为解析不出名字就把不合格的图留着等进建模。"""
    chat.replies.extend([REJECT_VAGUE, APPROVE])

    result = vision.review(project_db, session, project, generated, chat=chat, generate=draw)

    assert result.regenerated == 1
    assert {one["variant"] for one in draw.calls[4:]} == set(views.BY_CODE)


def test_不给generate就只审不重生(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    generated: characters.Character,
) -> None:
    """重生要花额度，调用方得明确表示它接受这笔开销。"""
    chat.replies.append(REJECT_BACK)

    result = vision.review(project_db, session, project, generated, chat=chat)

    assert result.rejected
    assert result.regenerated == 0
    assert len(draw.calls) == 4


def test_重生够了次数就转人工(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    generated: characters.Character,
) -> None:
    """模型连着三次都过不了的问题，多半得人改 prompt 或换姿势模版才解得开。"""
    chat.replies.extend([REJECT_BACK] * 8)

    result = vision.review(project_db, session, project, generated, chat=chat, generate=draw)

    assert result.manual
    assert result.regenerated == vision.MAX_AUTO_REGENERATIONS
    assert result.attempt == vision.MAX_AUTO_REGENERATIONS + 1
    events = task_events.history(project_db, generated.id)
    manual = [one for one in events if one.event == "views_review_manual"]
    assert manual[-1].level == "warning"
    meta = json.loads(characters.meta_path(project, generated).read_text(encoding="utf-8"))
    assert meta["views"]["review"]["manual"] is True


def test_评审不动状态也不定稿(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    """APPROVE 只表示审校没发现问题，定稿仍要人来选。"""
    chat.replies.append(APPROVE)

    vision.review(project_db, session, project, generated, chat=chat)

    assert generated.state == characters.VIEWS_GENERATED
    assert characters.view_paths(generated) == {}


def test_点名解析不把background当成背面(project_db: Session) -> None:
    """`back` 不卡词边界的话，一句 pure white background 就会被读成在点名背面那张。"""
    from atelier.agents.parsing import parse_verdict

    verdict = parse_verdict(REJECT_VAGUE, "VIEW-CHECK")
    assert "background" in verdict.text
    assert not vision._named_in(verdict.text, "back")
