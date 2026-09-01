"""记忆的作用域：项目级注入所有人，角色级只跟着自己那个角色。

要钉的是**不串味**：在「赤瞳」设定里聊出的偏好，不能跟着进下一个角色的提示词——它不仅无用，
还会被模型当成本项目的通例写进新设定，用户得花一轮把它推翻。作用域由会话的对焦对象定，不由
模型选的标记决定。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from atelier.agents import conversation as engine
from atelier.agents import parsing
from atelier.assets import characters
from atelier.assets import memory as memory_files
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import Character, Conversation
from tests.conftest import ScriptedChat, bind_text_model
from tests.test_characters import ready

WRITER = "spec_writer"
DESIGNER = "game_designer"

CHARACTER_REPLY = """给你一版。

[草稿开始: 赤瞳角色设定.md]
# 赤瞳
双尾、红瞳。
[草稿结束]

[角色记忆]
preference: 尾巴要 2 条且彼此分离
taboo: 不要机械义肢
"""

PROJECT_REPLY = """拟一版规范。

[草稿开始: art-bible.md]
# 视觉规范
冷光下的湿滑金属。
[草稿结束]

[项目记忆]
preference: 喜欢冷色调
"""


@pytest.fixture
def candidates(session: Session) -> None:
    bind_text_model(session, WRITER)
    bind_text_model(session, DESIGNER, code="ark")


def make(project_db: Session, project: ProjectRef, name: str) -> Character:
    ready(project)
    return characters.create(project_db, project, name)


def prefs(project: ProjectRef, character: Character | None = None) -> list[str]:
    """某个对象目录里那份偏好文件写着什么。"""
    base = project.absolute(character.dir_name) if character else project.dir
    return [one.content for one in memory_files.read_preferences(base)]


def talk(project_db: Session, character: Character) -> Conversation:
    return engine.start(
        project_db,
        agent_code=WRITER,
        target_kind="character",
        target_ref=character.id,
        title="设定对焦",
    )


def settle(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    conversation: Conversation,
    reply: str,
) -> None:
    engine.send(
        project_db,
        session,
        project,
        conversation,
        "先出一版",
        chat=ScriptedChat(reply),
        stream=False,
    )
    engine.commit(project_db, project, conversation)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def test_角色记忆块也认() -> None:
    """`spec_writer` 的输出契约写的是 `[角色记忆]`，不认就等于这些条目全丢了。"""
    items = parsing.parse_memories(CHARACTER_REPLY)

    assert [(one.kind, one.content) for one in items] == [
        ("preference", "尾巴要 2 条且彼此分离"),
        ("taboo", "不要机械义肢"),
    ]


def test_两个记忆块都写了就都收() -> None:
    text = f"{PROJECT_REPLY}\n[角色记忆]\ntaboo: 不要机械义肢\n"

    contents = [one.content for one in parsing.parse_memories(text)]

    assert contents == ["喜欢冷色调", "不要机械义肢"]


# --------------------------------------------------------------------------- #
# 作用域归属
# --------------------------------------------------------------------------- #


def test_角色会话聊出的记忆归这个角色(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    character = make(project_db, project, "赤瞳")
    conversation = talk(project_db, character)

    settle(project_db, project, session, conversation, CHARACTER_REPLY)

    assert prefs(project, character) == ["尾巴要 2 条且彼此分离", "不要机械义肢"]
    assert prefs(project) == []  # 一条都没漏进项目那份


def test_项目会话聊出的记忆是项目级(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    conversation = engine.start(project_db, agent_code=DESIGNER, target_kind="project")

    settle(project_db, project, session, conversation, PROJECT_REPLY)

    assert prefs(project) == ["喜欢冷色调"]


def test_一个角色的偏好不跟着进另一个角色(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    """这是这一层的要点：串味的记忆会被当成本项目的通例写进新角色的设定。"""
    red = make(project_db, project, "赤瞳")
    settle(project_db, project, session, talk(project_db, red), CHARACTER_REPLY)
    blue = characters.create(project_db, project, "青瞳")

    seen = [one.content for one in engine.enabled_memories(project_db, project, blue.id)]

    assert seen == []


def test_下一个角色的提示词里没有上一个的偏好(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    """只看库里分没分不够，要看模型真正收到的那份上下文里有没有它。"""
    red = make(project_db, project, "赤瞳")
    settle(project_db, project, session, talk(project_db, red), CHARACTER_REPLY)
    blue = characters.create(project_db, project, "青瞳")
    chat = ScriptedChat("好的，先聊头部。")

    engine.send(
        project_db,
        session,
        project,
        talk(project_db, blue),
        "开始聊青瞳",
        chat=chat,
        stream=False,
    )

    assert "机械义肢" not in chat.system_of_last


def test_项目级记忆两个角色都看得见(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    conversation = engine.start(project_db, agent_code=DESIGNER, target_kind="project")
    settle(project_db, project, session, conversation, PROJECT_REPLY)
    red = make(project_db, project, "赤瞳")

    seen = [one.content for one in engine.enabled_memories(project_db, project, red.id)]

    assert seen == ["喜欢冷色调"]


def test_模型指错标记也按会话归属(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    """让模型自己挑作用域的话，它在角色会话里写一句 `[项目记忆]` 就污染了全项目。"""
    character = make(project_db, project, "赤瞳")
    conversation = talk(project_db, character)
    mislabeled = CHARACTER_REPLY.replace(parsing.CHARACTER_MEMORY_MARKER, parsing.MEMORY_MARKER)

    settle(project_db, project, session, conversation, mislabeled)

    assert engine.enabled_memories(project_db, project) == []
    assert len(engine.enabled_memories(project_db, project, character.id)) == 2


# --------------------------------------------------------------------------- #
# 去重
# --------------------------------------------------------------------------- #


def test_同一句话对不同角色各算一条(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    """两个角色可以各自要求「尾巴 2 条」，这不是重复，是两条各自成立的约束。

    id 是内容哈希，所以这两条的 id 一样；各自成立体现在它们落在各自的目录里。
    """
    red = make(project_db, project, "赤瞳")
    blue = characters.create(project_db, project, "青瞳")

    first = engine.write_memory(
        project_db, project, "preference", "尾巴要 2 条", character_ref=red.id
    )
    second = engine.write_memory(
        project_db, project, "preference", "尾巴要 2 条", character_ref=blue.id
    )

    assert first is not None
    assert second is not None
    assert prefs(project, red) == prefs(project, blue) == ["尾巴要 2 条"]


def test_项目级已经有了就不再写角色副本(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    """两条一模一样的记忆同时注入，用户在设置页关掉其中一条会发现它依旧生效。"""
    red = make(project_db, project, "赤瞳")
    engine.write_memory(project_db, project, "preference", "喜欢冷色调")

    again = engine.write_memory(
        project_db, project, "preference", "喜欢冷色调", character_ref=red.id
    )

    assert again is None
    assert prefs(project, red) == []


def test_角色级不挡住项目级(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    """反过来要放行：一条对全项目成立的偏好不该因为某个角色先说过就写不进去。"""
    red = make(project_db, project, "赤瞳")
    engine.write_memory(project_db, project, "preference", "喜欢冷色调", character_ref=red.id)

    promoted = engine.write_memory(project_db, project, "preference", "喜欢冷色调")

    assert promoted is not None
    assert prefs(project) == ["喜欢冷色调"]


def test_停用的记忆不再注入(
    project_db: Session, project: ProjectRef, session: Session, candidates: None
) -> None:
    red = make(project_db, project, "赤瞳")
    row = engine.write_memory(
        project_db, project, "preference", "尾巴要 2 条", character_ref=red.id
    )
    assert row is not None
    memory_files.update_preference(
        project.absolute(red.dir_name),
        row.id,
        scope=memory_files.SCOPE_CHARACTER,
        enabled=False,
    )

    assert engine.enabled_memories(project_db, project, red.id) == []
