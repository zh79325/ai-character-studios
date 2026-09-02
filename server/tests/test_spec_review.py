"""设定评审：单次调用编排、裁决落库、REJECT 自动重生、转人工。

这一层要钉的是「凭什么」：裁决全文留在 `task_events` 里，约束清单同时进库行与 `meta.json`，
自动重生有上限。模型一律用 `ScriptedChat` 替掉——要验的是编排，不是模型会不会好好说话。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from atelier.agents import conversation as conv
from atelier.agents import review
from atelier.agents.parsing import VerdictError
from atelier.assets import characters
from atelier.assets.projects import ProjectRef
from atelier.db import task_events
from atelier.db.project_models import Character, Conversation
from atelier.errors import Conflict
from tests.conftest import ScriptedChat, action_reply, bind_text_model
from tests.test_characters import ready

APPROVE_REPLY = action_reply(
    "### 缺失维度\n无\n\n### 硬性约束清单\n- 尾巴 = 2 条，彼此分离\n- 眼睛 = 红色发光",
    reason="设定审校完成",
    payload={
        "verdict": {
            "token": "SPEC-CHECK",
            "decision": "APPROVE",
            "sections": {"缺失维度": [], "硬性约束清单": []},
            "constraints": [
                {"item": "尾巴", "value": "2 条，彼此分离"},
                {"item": "眼睛", "value": "红色发光"},
            ],
        }
    },
)

REJECT_REPLY = action_reply(
    "### 缺失维度\n- 环境设定\n\n### 模糊表述\n- 原文「深色鳞片」→ 应写明：具体色值",
    reason="设定审校完成",
    payload={
        "verdict": {
            "token": "SPEC-CHECK",
            "decision": "REJECT",
            "sections": {
                "缺失维度": ["环境设定"],
                "模糊表述": ["原文「深色鳞片」→ 应写明：具体色值"],
                "硬性约束清单": [],
            },
            "constraints": [{"item": "尾巴", "value": "2 条"}],
        }
    },
)

CONCERNS_REPLY = action_reply(
    "### 模糊表述\n- 原文「发光的眼睛」→ 应写明：发光强度",
    reason="设定审校完成",
    payload={
        "verdict": {
            "token": "SPEC-CHECK",
            "decision": "CONCERNS",
            "sections": {
                "模糊表述": ["原文「发光的眼睛」→ 应写明：发光强度"],
                "硬性约束清单": [],
            },
            "constraints": [{"item": "眼睛", "value": "红色发光"}],
        }
    },
)

DRAFT_REPLY = action_reply(
    "改好了，补上了环境设定。",
    action="ask_user",
    reason="设定草稿需要用户确认",
    payload={
        "drafts": [
            {
                "target_path": "docs/角色定稿.md",
                "content": "# 赤瞳\n双尾、红瞳、三指利爪，栖息在废弃电厂。\n",
            }
        ]
    },
)


@pytest.fixture
def candidates(session: Session) -> None:
    """评审与写手各备一个候选，落在不同 provider 上免得撞 code。"""
    bind_text_model(session, review.REVIEWER)
    bind_text_model(session, "spec_writer", code="ark")


def make(project_db: Session, project: ProjectRef, name: str = "赤瞳") -> Character:
    ready(project)
    return characters.create(project_db, project, name)


def write_spec(project: ProjectRef, character: Character, text: str) -> None:
    """磁盘上已有一份定稿，并挂到库行上。"""
    relative = characters.spec_target(character)
    path = project.absolute(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    character.spec_path = relative


def talk(project_db: Session, character: Character) -> Conversation:
    return conv.start(
        project_db,
        agent_code="spec_writer",
        target_kind="character",
        target_ref=character.id,
        title="设定对焦",
    )


# --------------------------------------------------------------------------- #
# 审的是哪一版
# --------------------------------------------------------------------------- #


def test_有草稿就审草稿而不是磁盘上的旧定稿(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    """用户刚聊出来的新版进不了评审，评审就成了对着旧文件表态。"""
    character = make(project_db, project)
    write_spec(project, character, "# 赤瞳\n这是磁盘上的旧版。\n")
    project_db.commit()
    conversation = talk(project_db, character)
    chat = ScriptedChat(DRAFT_REPLY, APPROVE_REPLY)
    conv.send(project_db, session, project, conversation, "补一下环境", chat=chat, stream=False)

    review.review_once(project_db, session, project, character, chat=chat)

    reviewed = chat.system_of_last
    assert "废弃电厂" in reviewed
    assert "磁盘上的旧版" not in reviewed


def test_没草稿时审磁盘上那份(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    character = make(project_db, project)
    write_spec(project, character, "# 赤瞳\n磁盘上这份。\n")
    project_db.commit()

    review.review_once(project_db, session, project, character, chat=ScriptedChat(APPROVE_REPLY))

    assert character.hard_constraints["spec_path"] == character.spec_path


def test_一个字都没有就没什么可审(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    character = make(project_db, project)

    with pytest.raises(Conflict, match="还没有设定内容可审"):
        review.review_once(
            project_db, session, project, character, chat=ScriptedChat(APPROVE_REPLY)
        )


def test_评审看得见项目视觉规范(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    """art bible 是判定风格冲突的唯一依据，不给它就只能凭印象说话。"""
    character = make(project_db, project)
    write_spec(project, character, "# 赤瞳\n红瞳。\n")
    project_db.commit()
    chat = ScriptedChat(APPROVE_REPLY)

    review.review_once(project_db, session, project, character, chat=chat)

    asked = chat.calls[-1][-1]["content"]
    assert "冷光金属" in asked


# --------------------------------------------------------------------------- #
# 裁决落库
# --------------------------------------------------------------------------- #


def test_裁决全文原样进事件时间线(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    """摘成一句「APPROVE」等于把证据丢了，日后回答不了「当时凭什么过的」。"""
    character = make(project_db, project)
    write_spec(project, character, "# 赤瞳\n红瞳。\n")
    project_db.commit()

    review.review_once(project_db, session, project, character, chat=ScriptedChat(CONCERNS_REPLY))

    event = task_events.history(project_db, character.id)[-1]
    assert event.event == "spec_reviewed"
    assert event.message == CONCERNS_REPLY.strip()
    assert event.level == "warning"
    assert event.payload["decision"] == "CONCERNS"
    assert event.payload["sections"]["模糊表述"] == ["原文「发光的眼睛」→ 应写明：发光强度"]


def test_约束清单同时落库行与素材台账(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    character = make(project_db, project)
    write_spec(project, character, "# 赤瞳\n红瞳。\n")
    project_db.commit()

    result = review.review(
        project_db, session, project, character, chat=ScriptedChat(APPROVE_REPLY)
    )

    assert [one.item for one in result.verdict.constraints] == ["尾巴", "眼睛"]
    assert character.hard_constraints["items"] == [
        {"item": "尾巴", "value": "2 条，彼此分离"},
        {"item": "眼睛", "value": "红色发光"},
    ]
    meta = json.loads(characters.meta_path(project, character).read_text(encoding="utf-8"))
    assert meta["character"]["hard_constraints"] == character.hard_constraints["items"]


def test_新一版的清单整份换掉旧的(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    """清单是对某一版设定的翻译；设定改了还留着旧条目，后续每张图都会按一条不成立的要求判。"""
    character = make(project_db, project)
    write_spec(project, character, "# 赤瞳\n红瞳。\n")
    project_db.commit()
    review.review_once(project_db, session, project, character, chat=ScriptedChat(APPROVE_REPLY))

    review.review_once(project_db, session, project, character, chat=ScriptedChat(CONCERNS_REPLY))

    assert character.hard_constraints["items"] == [{"item": "眼睛", "value": "红色发光"}]
    assert character.hard_constraints["decision"] == "CONCERNS"


def test_通过了也不动状态与门禁(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    """APPROVE 只表示审校没发现问题；放行是人的事，自动裁决替人拍板等于把责任推给模型。"""
    character = make(project_db, project)
    write_spec(project, character, "# 赤瞳\n红瞳。\n")
    project_db.commit()

    result = review.review(
        project_db, session, project, character, chat=ScriptedChat(APPROVE_REPLY)
    )

    assert result.approved is True
    assert character.state == characters.SPEC_DRAFTING
    assert character.gate_spec_confirmed_at is None


def test_没按契约说话是格式事故不是驳回(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    """当成 REJECT 会变成一次没有理由的驳回，用户看不出该改设定还是该重试。"""
    character = make(project_db, project)
    write_spec(project, character, "# 赤瞳\n红瞳。\n")
    project_db.commit()

    with pytest.raises(VerdictError):
        review.review_once(
            project_db, session, project, character, chat=ScriptedChat("我看着挺好的。")
        )

    event = task_events.history(project_db, character.id)[-1]
    assert event.event == "spec_review_unparsable"
    assert event.level == "error"
    assert character.hard_constraints == {}


# --------------------------------------------------------------------------- #
# 自动重生
# --------------------------------------------------------------------------- #


def test_驳回后把理由发回写手重生(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    character = make(project_db, project)
    write_spec(project, character, "# 赤瞳\n深色鳞片。\n")
    project_db.commit()
    conversation = talk(project_db, character)
    chat = ScriptedChat(REJECT_REPLY, DRAFT_REPLY, APPROVE_REPLY)

    result = review.review(
        project_db, session, project, character, conversation=conversation, chat=chat
    )

    assert result.decision == "APPROVE"
    assert result.regenerated == 1
    assert result.attempt == 2
    assert result.manual is False
    retry = conv.messages_of(project_db, conversation.id)[0].content
    assert "环境设定" in retry
    assert "深色鳞片" in retry


def test_重生的话里不带硬性约束清单() -> None:
    """清单是评审给生图的产出；混进来写手会把「尾巴 = 2 条」抄成设定里的一句话。"""
    from atelier.agents.parsing import parse_verdict

    reasons = review.reasons_of(parse_verdict(REJECT_REPLY))

    assert "缺失维度" in reasons
    assert "尾巴" not in reasons


def test_没有会话就只审一次(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    """重生得有个会话承载新的一轮，硬造一个会跟用户自己那场对不上号。"""
    character = make(project_db, project)
    write_spec(project, character, "# 赤瞳\n深色鳞片。\n")
    project_db.commit()

    result = review.review(project_db, session, project, character, chat=ScriptedChat(REJECT_REPLY))

    assert result.decision == "REJECT"
    assert result.regenerated == 0
    assert result.manual is False


def test_重生到上限就转人工(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    """第四次多半还是同样的答案，继续烧 token 不如把问题摆给用户看。"""
    character = make(project_db, project)
    write_spec(project, character, "# 赤瞳\n深色鳞片。\n")
    project_db.commit()
    conversation = talk(project_db, character)
    chat = ScriptedChat(
        REJECT_REPLY,
        DRAFT_REPLY,
        REJECT_REPLY,
        DRAFT_REPLY,
        REJECT_REPLY,
        DRAFT_REPLY,
        REJECT_REPLY,
    )

    result = review.review(
        project_db, session, project, character, conversation=conversation, chat=chat
    )

    assert result.manual is True
    assert result.regenerated == review.MAX_AUTO_REGENERATIONS
    assert result.attempt == review.MAX_AUTO_REGENERATIONS + 1
    events = [one.event for one in task_events.history(project_db, character.id)]
    assert events[-1] == "spec_review_manual"
    assert events.count("spec_reviewed") == review.MAX_AUTO_REGENERATIONS + 1
