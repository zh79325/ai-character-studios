"""单张四宫格视觉评审与整图自动重生。"""

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
- 四个格位、角色一致性、背景和建模轮廓均符合

### 修正建议
- 无
"""

CONCERNS = """VIEW-CHECK: CONCERNS

### 检查清单
- 右上角度略偏

### 修正建议
- 右上保持 30°
"""

REJECT = """VIEW-CHECK: REJECT

### 检查清单
- 左下背面附属结构粘连

### 修正建议
- 左下背面重画，整张四宫格重新生成
"""

BABBLE = """这张图整体不错。
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
    character = stage_render(project_db, project)
    views.generate_views(project_db, session, project, character, generate=draw)
    return character


def parts_of(call: list[dict[str, Any]], kind: str) -> list[Any]:
    content = call[-1]["content"]
    assert isinstance(content, list)
    return [one for one in content if one["type"] == kind]


def asked(chat: ScriptedChat, index: int = -1) -> str:
    return "".join(one["text"] for one in parts_of(chat.calls[index], "text"))


def images_in(chat: ScriptedChat, index: int = -1) -> list[str]:
    return [one["image_url"]["url"] for one in parts_of(chat.calls[index], "image_url")]


def set_mode(ref: ProjectRef, mode: str) -> None:
    config = projects.read_config(ref.dir)
    config.review_mode = mode  # type: ignore[assignment]
    projects.write_config(ref.dir, config)


def test_没生成四宫格就不给评审(
    project: ProjectRef, project_db: Session, session: Session, chat: ScriptedChat
) -> None:
    character = make(project_db, project)
    spec_on_disk(project, character)
    with pytest.raises(Conflict, match="才能评审四视图"):
        vision.review(project_db, session, project, character, chat=chat)


def test_只取最新sheet且旧分图不进入评审(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    draw: ScriptedDraw,
    generated: characters.Character,
) -> None:
    first = vision.shots(project_db, project, generated)[0]
    views.generate_views(project_db, session, project, generated, generate=draw)
    current = vision.shots(project_db, project, generated)
    assert len(current) == 1
    assert current[0].variant == views.SHEET_CODE
    assert current[0].file_path != first.file_path


def test_四宫格不在磁盘上就拒绝(
    project: ProjectRef, project_db: Session, generated: characters.Character
) -> None:
    picked = vision.shots(project_db, project, generated)
    project.absolute(picked[0].file_path).unlink()
    with pytest.raises(Conflict, match="重新生成一张"):
        vision.shots(project_db, project, generated)


def test_单图和四格机器读数都进请求且检查披风(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    chat.replies.append(APPROVE)
    result = vision.review(project_db, session, project, generated, chat=chat)
    assert result.approved
    assert len(chat.calls) == 1
    assert len(images_in(chat)) == 1
    assert images_in(chat)[0].startswith("data:image/png;base64,")
    request = asked(chat)
    assert "左上正面" in request
    assert "右上右侧 30°" in request
    assert "左下背面" in request
    assert "右下左侧 30°" in request
    assert "边缘匹配率" in request
    assert "披风、斗篷" in request


def test_机器问题标明具体格位给模型看(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    shot = vision.shots(project_db, project, generated)[0]
    project.absolute(shot.file_path).write_bytes(gray_png())
    chat.replies.append(REJECT)
    vision.review(project_db, session, project, generated, chat=chat)
    request = asked(chat)
    assert "机器判定问题" in request
    assert "左上正面" in request
    assert "目标纯色 #FFFFFF" in request


def test_设定原文也挂进上下文(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    chat.replies.append(APPROVE)
    vision.review(project_db, session, project, generated, chat=chat)
    assert "双尾、红瞳" in chat.calls[-1][0]["content"]


@pytest.mark.parametrize("mode", [vision.LEAN, vision.FULL])
def test_full与lean都只整图审一次(
    mode: str,
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    set_mode(project, mode)
    chat.replies.append(APPROVE)
    result = vision.review(project_db, session, project, generated, chat=chat)
    assert result.mode == mode
    assert len(chat.calls) == 1
    assert len(result.verdicts) == 1
    assert result.verdicts[0].variants == (views.SHEET_CODE,)


def test_solo不调用评审(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    set_mode(project, vision.SOLO)
    result = vision.review(project_db, session, project, generated, chat=chat)
    assert result.skipped
    assert result.verdicts == ()
    assert chat.calls == []


def test_裁决全文和sheet定位进事件(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    chat.replies.append(CONCERNS)
    vision.review(project_db, session, project, generated, chat=chat)
    reviewed = [
        one
        for one in task_events.history(project_db, generated.id)
        if one.event == "views_reviewed"
    ]
    assert reviewed[-1].message == CONCERNS.strip()
    assert reviewed[-1].payload["variants"] == [views.SHEET_CODE]
    assert len(reviewed[-1].payload["generation_ids"]) == 1


def test_首行不是裁决就当格式事故(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    chat.replies.append(BABBLE)
    with pytest.raises(VerdictError):
        vision.review(project_db, session, project, generated, chat=chat)
    events = task_events.history(project_db, generated.id)
    assert [one for one in events if one.event == "views_review_unparsable"][-1].level == "error"


def test_评审结论进meta且保留单图参数快照(
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
    assert len(meta["views"]["images"]) == 1
    assert meta["views"]["images"][0]["variant"] == views.SHEET_CODE


def test_REJECT后整张重生再评审(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    generated: characters.Character,
) -> None:
    chat.replies.extend([REJECT, APPROVE])
    before = vision.shots(project_db, project, generated)[0].file_path
    initial_calls = len(draw.calls)
    result = vision.review(project_db, session, project, generated, chat=chat, generate=draw)
    assert result.regenerated == 1
    assert result.approved
    assert len(draw.calls) == initial_calls + 1
    assert vision.shots(project_db, project, generated)[0].file_path != before


def test_不给generate就只审不重生(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    generated: characters.Character,
) -> None:
    chat.replies.append(REJECT)
    before = len(draw.calls)
    result = vision.review(project_db, session, project, generated, chat=chat)
    assert result.rejected
    assert result.regenerated == 0
    assert len(draw.calls) == before


def test_重生到上限转人工(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    draw: ScriptedDraw,
    generated: characters.Character,
) -> None:
    chat.replies.extend([REJECT] * (vision.MAX_AUTO_REGENERATIONS + 1))
    result = vision.review(project_db, session, project, generated, chat=chat, generate=draw)
    assert result.manual
    assert result.regenerated == vision.MAX_AUTO_REGENERATIONS
    assert result.attempt == vision.MAX_AUTO_REGENERATIONS + 1


def test_评审不定稿(
    project: ProjectRef,
    project_db: Session,
    session: Session,
    chat: ScriptedChat,
    generated: characters.Character,
) -> None:
    chat.replies.append(APPROVE)
    vision.review(project_db, session, project, generated, chat=chat)
    assert generated.state == characters.VIEWS_GENERATED
    assert characters.view_paths(generated) == {}
