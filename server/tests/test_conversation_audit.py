"""会话审计：开关、落盘位置、Request 先写 / Response 后追加、脱敏。

模型一律用假实现替掉：这一层验的是审计文件写成什么样，不是模型会不会好好说话。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from atelier.agents import conversation as engine
from atelier.assets import layout
from atelier.assets import projects as projects_mod
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import Character, Conversation
from atelier.providers.base import Candidate
from atelier.providers.text_chat import ChatReply
from tests.conftest import ScriptedChat, bind_text_model

DESIGNER = "game_designer"
WRITER = "spec_writer"


@pytest.fixture
def candidate(session: Session) -> None:
    bind_text_model(session, DESIGNER)


@pytest.fixture
def writer_candidate(session: Session) -> None:
    bind_text_model(session, WRITER)


def enable_audit(project: ProjectRef, *, enabled: bool = True) -> None:
    config = projects_mod.read_config(project.dir)
    config.conversation_audit = enabled
    projects_mod.write_config(project.dir, config)


def audit_dir(base: Path) -> Path:
    return base / "tmp" / "conversation"


def audit_files(base: Path) -> list[Path]:
    target = audit_dir(base)
    return sorted(target.glob("*.md")) if target.is_dir() else []


def start_project_talk(project_db: Session) -> Conversation:
    return engine.start(project_db, agent_code=DESIGNER, target_kind="project", title="立项对焦")


def make_character(project_db: Session, project: ProjectRef, group: str = "玩家角色") -> Character:
    dir_name = f"characters/{group}/赤瞳"
    asset_dir = project.absolute(dir_name)
    layout.ensure_asset_dirs(asset_dir)
    layout.write_model_marker(asset_dir, "赤瞳")
    character = Character(id="c-chitong", name="赤瞳", dir_name=dir_name)
    project_db.add(character)
    project_db.commit()
    return character


def send(
    project_db: Session,
    session: Session,
    project: ProjectRef,
    conversation: Conversation,
    text: str,
    chat: Any,
    *,
    stream: bool = False,
) -> engine.TurnResult:
    return engine.send(project_db, session, project, conversation, text, chat=chat, stream=stream)


# --------------------------------------------------------------------------- #
# 开关
# --------------------------------------------------------------------------- #


def test_关掉审计时一个文件也不产生(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    """默认关闭：不建目录、不写文件，也不改变这一轮的结果。"""
    conversation = start_project_talk(project_db)

    result = send(project_db, session, project, conversation, "开聊", ScriptedChat("好的。"))

    assert result.content == "好的。"
    assert not audit_dir(project.dir).exists()


# --------------------------------------------------------------------------- #
# 落盘位置
# --------------------------------------------------------------------------- #


def test_项目会话写进项目目录的tmp_conversation(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    enable_audit(project)
    conversation = start_project_talk(project_db)

    result = send(project_db, session, project, conversation, "开聊", ScriptedChat("好的。"))

    files = audit_files(project.dir)
    assert len(files) == 1
    # 文件名带小时与轮次，便于按时间快速定位
    assert files[0].name.endswith(f"-turn-{result.turn_no}.md")
    stamp, _, _ = files[0].name.partition("-turn-")
    day, hour = stamp.split("-")
    assert len(day) == 8 and day.isdigit()
    assert len(hour) == 2 and hour.isdigit()


def test_角色会话写进那个角色自己的目录(
    project_db: Session, project: ProjectRef, session: Session, writer_candidate: None
) -> None:
    """角色在分组下嵌了几层也要跟着走：审计得摆在这个角色自己的 tmp/ 里。"""
    enable_audit(project)
    character = make_character(project_db, project)
    conversation = engine.start(
        project_db, agent_code=WRITER, target_kind="character", target_ref=character.id
    )

    send(project_db, session, project, conversation, "开聊", ScriptedChat("好的。"))

    assert len(audit_files(project.absolute(character.dir_name))) == 1
    # 项目根不该跟着留一份，否则角色的记录就散在两处
    assert not audit_dir(project.dir).exists()


def test_每一轮各自一份文件(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    enable_audit(project)
    conversation = start_project_talk(project_db)
    chat = ScriptedChat("第一轮回答", "第二轮回答")

    send(project_db, session, project, conversation, "第一轮", chat)
    send(project_db, session, project, conversation, "第二轮", chat)

    files = audit_files(project.dir)
    assert [one.name.split("-turn-")[1] for one in files] == ["2.md", "4.md"]


# --------------------------------------------------------------------------- #
# 文件内容
# --------------------------------------------------------------------------- #


def test_请求先落盘再调模型_回答之后才追加(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    """审计的意义在于「发出去的到底是什么」，所以 Request 必须在调用之前就已经在磁盘上。"""
    enable_audit(project)
    conversation = start_project_talk(project_db)
    seen: dict[str, str] = {}

    def chat(_candidate: Candidate, messages: Any, **_kwargs: Any) -> ChatReply:
        seen["at_call"] = audit_files(project.dir)[0].read_text(encoding="utf-8")
        assert messages
        return ChatReply(
            content="想好了。", prompt_tokens=10, completion_tokens=20, total_tokens=30
        )

    send(project_db, session, project, conversation, "开聊", chat)

    assert "### Request" in seen["at_call"]
    assert "开聊" in seen["at_call"]
    # 调用那一刻还没有回答，不能凭空先写一段
    assert "### Response" not in seen["at_call"]

    final = audit_files(project.dir)[0].read_text(encoding="utf-8")
    assert final.startswith("# LLM 对话审计")
    assert f"- 会话：{conversation.id}" in final
    assert "- 目标：project" in final
    assert f"- Agent：{DESIGNER}" in final
    assert "## 调用 1：主回答" in final
    assert "- Provider：bailian/qwen-plus" in final
    assert "### Response" in final
    assert "想好了。" in final
    assert "- Total tokens：30" in final


def test_同一轮的折叠调用与主回答按顺序进同一个文件(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    candidate: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """折叠也是一次真实的 LLM 调用，漏掉它这一轮就复查不全。"""
    enable_audit(project)
    conversation = start_project_talk(project_db)
    send(project_db, session, project, conversation, "第一轮", ScriptedChat("第一轮回答"))
    monkeypatch.setattr("atelier.agents.context.fold_plan", lambda _assembled: (1,), raising=False)
    chat = ScriptedChat("前情摘要", "第二轮回答")

    send(project_db, session, project, conversation, "第二轮", chat)

    text = audit_files(project.dir)[-1].read_text(encoding="utf-8")
    assert text.index("## 调用 1：上下文折叠") < text.index("## 调用 2：主回答")
    assert "前情摘要" in text
    assert "第二轮回答" in text


def test_失败与中断都留下错误和已经收到的片段(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    enable_audit(project)
    conversation = start_project_talk(project_db)

    def chat(
        _candidate: Candidate,
        _messages: Any,
        *,
        on_delta: Any = None,
        **_kwargs: Any,
    ) -> ChatReply:
        if on_delta is not None:
            on_delta("已经出了半句")
        raise RuntimeError("模型这次没答完")

    with pytest.raises(RuntimeError, match="模型这次没答完"):
        send(project_db, session, project, conversation, "开聊", chat, stream=True)

    text = audit_files(project.dir)[0].read_text(encoding="utf-8")
    assert "### Error" in text
    assert "模型这次没答完" in text
    assert "### Partial Response" in text
    assert "已经出了半句" in text


def test_审计不写凭证与图片正文(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    """凭证与 base64 图片一旦落进项目目录就等于被同步、被提交出去。"""
    enable_audit(project)
    conversation = start_project_talk(project_db)
    image = "data:image/png;base64,aGVsbG8="

    def chat(_candidate: Candidate, messages: Any, **_kwargs: Any) -> ChatReply:
        assert messages
        return ChatReply(content="看到了。")

    send(project_db, session, project, conversation, f"看这张图 {image}", chat)
    audit_path = audit_files(project.dir)[0]
    # 直接验记录器本身：会话上下文目前只有纯文本，凭证与图片是它必须挡住的两类输入
    audit = engine.audit.TurnAudit.create(
        project.dir,
        conversation_id=conversation.id,
        turn_no=99,
        target="project",
        agent_code=DESIGNER,
    )
    audit.write_request(
        "主回答",
        Candidate(
            provider_model_id=1,
            provider_code="bailian",
            provider_name="bailian 账号",
            model_id="qwen-plus",
            driver="openai_compat",
            endpoint="https://example.invalid/v1",
            api_key="sk-secret",
            priority=100,
            sort_no=0,
        ),
        [
            {"role": "system", "api_key": "sk-secret", "content": "守规矩"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": image}}]},
        ],
    )

    assert "sk-secret" not in audit.path.read_text(encoding="utf-8")
    assert "***" in audit.path.read_text(encoding="utf-8")
    assert "data URL omitted" in audit.path.read_text(encoding="utf-8")
    assert "aGVsbG8=" not in audit.path.read_text(encoding="utf-8")
    assert audit_path.read_text(encoding="utf-8").count("### Response") == 1
