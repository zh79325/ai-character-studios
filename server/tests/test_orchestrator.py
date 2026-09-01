"""编排骨架：阶段、白名单、显式指派与粘性焦点。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from atelier.agents import orchestrator
from atelier.assets import characters as character_assets
from atelier.db.project_models import Character, Conversation


def make_character(project_db: Session, name: str = "赤瞳") -> Character:
    character = Character(id=f"c-{name}", name=name, dir_name=f"characters/{name}")
    project_db.add(character)
    project_db.commit()
    return character


def test_阶段表每一段都能按code取回() -> None:
    for one in orchestrator.STAGES:
        assert orchestrator.stage(one.code) is one


def test_子agent白名单里只有单次调用型的那几个() -> None:
    """主 Agent 只准派活给干一件事就走的 Agent，派给另一个会话型 Agent 等于两个人抢麦。"""
    for one in orchestrator.STAGES:
        assert one.director not in one.crew


def test_没确认设定就还在设定阶段(project_db: Session) -> None:
    character = make_character(project_db)

    assert orchestrator.stage_of(character) == "spec"


def test_设定门禁确认过就进渲染图阶段(project_db: Session) -> None:
    character = make_character(project_db)
    character.gate_spec_confirmed_at = datetime.now(UTC)

    assert orchestrator.stage_of(character) == "render"


def test_渲染图门禁确认过就进四视图阶段(project_db: Session) -> None:
    character = make_character(project_db)
    character.gate_spec_confirmed_at = datetime.now(UTC)
    character.gate_render_confirmed_at = datetime.now(UTC)

    assert orchestrator.stage_of(character) == "views"


def test_四视图确认之后不再往会话外跑(project_db: Session) -> None:
    """建模及之后不进会话，阶段停在四视图，不需要为它们再编一个 code。"""
    character = make_character(project_db)
    character.state = character_assets.VIEWS_CONFIRMED

    assert orchestrator.stage_of(character) == "views"


def test_当前说话人优先使用焦点agent() -> None:
    conversation = Conversation(
        id="conv-1",
        target_kind="character",
        target_ref="c-赤瞳",
        agent_code=orchestrator.DIRECTOR,
        focus_agent_code="spec_writer",
    )

    assert orchestrator.actor_for(conversation) == "spec_writer"


def test_显式指派优先于当前焦点并剥离mention() -> None:
    conversation = Conversation(
        id="conv-1",
        target_kind="character",
        target_ref="c-赤瞳",
        agent_code=orchestrator.DIRECTOR,
        focus_agent_code="spec_writer",
    )

    recipient = orchestrator.resolve_recipient(
        conversation,
        target_kind="character",
        stage_code="spec",
        text="@设定审校 帮我检查",
        recipient_agent_code="spec_reviewer",
    )

    assert recipient.agent_code == "spec_reviewer"
    assert recipient.source == "@"
    assert recipient.text == "帮我检查"


def test_无焦点时由总管收件() -> None:
    conversation = Conversation(
        id="conv-1",
        target_kind="project",
        agent_code=orchestrator.DIRECTOR,
    )

    recipient = orchestrator.resolve_recipient(
        conversation,
        target_kind="project",
        stage_code="project",
        text="下一步做什么",
    )

    assert recipient.agent_code == orchestrator.DIRECTOR
    assert recipient.source == "director"
