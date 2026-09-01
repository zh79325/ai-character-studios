"""共识落盘的格式层：`memory/` 与 `prompts/` 下那几份 Markdown 的读写。

这些文件是共识的唯一真相，而且允许用户直接手改，所以这里钉两件事：机器写出去的东西读回来
还是原样（往返），以及人手改过的文件不会因为格式不合机器口味就被吞掉。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from atelier.assets import layout
from atelier.assets import memory as memory_files

SCOPE = memory_files.SCOPE_PROJECT


def entry(kind: str, content: str, *, enabled: bool = True) -> memory_files.MemoryEntry:
    return memory_files.MemoryEntry(kind=kind, content=content, enabled=enabled)


# --------------------------------------------------------------------------- #
# 偏好与禁忌
# --------------------------------------------------------------------------- #


def test_偏好写出去再读回来还是原样(tmp_path: Path) -> None:
    entries = [
        entry("preference", "尾巴要 2 条"),
        entry("taboo", "不要粉色系"),
        entry("fact", "体量感参考《怪物猎人》", enabled=False),
    ]

    memory_files.write_preferences(tmp_path, entries, scope=SCOPE)

    assert memory_files.read_preferences(tmp_path) == entries


def test_勾选框就是启用开关(tmp_path: Path) -> None:
    """用户在编辑器里改这个方框就等于在设置页点了开关，两边得是同一件事。"""
    memory_files.write_preferences(
        tmp_path, [entry("preference", "冷色调", enabled=False)], scope=SCOPE
    )
    text = layout.preferences_path(tmp_path).read_text(encoding="utf-8")
    assert "- [ ] 冷色调" in text

    layout.preferences_path(tmp_path).write_text(
        text.replace("- [ ] 冷色调", "- [x] 冷色调"), encoding="utf-8"
    )

    assert memory_files.read_preferences(tmp_path)[0].enabled is True


def test_人手写的光秃秃列表项也算一条(tmp_path: Path) -> None:
    """让用户直接往文件里加一行是这套落盘的意义之一，非得补上方框才认就等于白落盘。"""
    layout.preferences_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    layout.preferences_path(tmp_path).write_text(
        "# 用户偏好与禁忌\n\n## taboo\n\n- 不要机械义肢\n", encoding="utf-8"
    )

    entries = memory_files.read_preferences(tmp_path)

    assert entries == [entry("taboo", "不要机械义肢")]


def test_认不出的小节整节跳过而不是错分类(tmp_path: Path) -> None:
    """把用户自己加的一节硬塞进某一类，会让「禁忌」被当成「偏好」注入，比读不到更糟。"""
    layout.preferences_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    layout.preferences_path(tmp_path).write_text(
        "## 我自己的备注\n\n- [x] 随手记的\n\n## preference\n\n- [x] 冷色调\n", encoding="utf-8"
    )

    assert [one.content for one in memory_files.read_preferences(tmp_path)] == ["冷色调"]


def test_同一句话在文件里出现两次只算一条(tmp_path: Path) -> None:
    layout.preferences_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    layout.preferences_path(tmp_path).write_text(
        "## preference\n\n- [x] 冷色调\n- [ ] 冷色调\n", encoding="utf-8"
    )

    assert memory_files.read_preferences(tmp_path) == [entry("preference", "冷色调")]


def test_文件不在就是还没聊出共识(tmp_path: Path) -> None:
    """读不到一律当空：新项目、刚 clone 下来的目录都是这个样子，不该抛。"""
    assert memory_files.read_preferences(tmp_path) == []
    assert memory_files.read_agent_memory(tmp_path, "spec_writer").is_empty()
    assert memory_files.read_agent_prompt(tmp_path, "spec_writer") is None
    assert memory_files.read_snippets(tmp_path) == []


def test_头部读不懂也不耽误读正文(tmp_path: Path) -> None:
    """frontmatter 被手改坏了只丢元数据，正文里那些共识照样得读出来。"""
    layout.preferences_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    layout.preferences_path(tmp_path).write_text(
        "---\nscope: [坏\n---\n\n## preference\n\n- [x] 冷色调\n", encoding="utf-8"
    )

    assert [one.content for one in memory_files.read_preferences(tmp_path)] == ["冷色调"]


def test_写一条重复的不写(tmp_path: Path) -> None:
    memory_files.add_preference(tmp_path, "preference", "尾巴要 2 条", scope=SCOPE)

    again = memory_files.add_preference(tmp_path, "preference", "尾巴要 2 条 ", scope=SCOPE)

    assert again is None
    assert len(memory_files.read_preferences(tmp_path)) == 1


def test_别处占了这个键就不写副本(tmp_path: Path) -> None:
    """角色级要让着项目级：同一句话两份都注入，用户关掉一条会发现它依旧生效。"""
    taken = {memory_files.memory_hash("preference", "冷色调")}

    assert (
        memory_files.add_preference(tmp_path, "preference", "冷色调", scope=SCOPE, taken=taken)
        is None
    )


def test_改了内容就换一个id(tmp_path: Path) -> None:
    """id 是内容哈希，所以改完要把新那条交回去，调用方才能继续寻址。"""
    first = memory_files.add_preference(tmp_path, "fact", "平台是 PC", scope=SCOPE)
    assert first is not None

    updated = memory_files.update_preference(
        tmp_path, first.id, scope=SCOPE, content="平台是 PC 与 Switch"
    )

    assert updated is not None and updated.id != first.id
    assert memory_files.read_preferences(tmp_path) == [updated]


def test_改成跟别的一样就并成一条(tmp_path: Path) -> None:
    first = memory_files.add_preference(tmp_path, "fact", "平台是 PC", scope=SCOPE)
    second = memory_files.add_preference(tmp_path, "fact", "平台是 Switch", scope=SCOPE)
    assert first is not None and second is not None

    memory_files.update_preference(tmp_path, second.id, scope=SCOPE, content="平台是 PC")

    assert memory_files.read_preferences(tmp_path) == [first]


def test_删不存在的一条不算改动(tmp_path: Path) -> None:
    memory_files.add_preference(tmp_path, "taboo", "不要齿轮", scope=SCOPE)

    assert memory_files.delete_preference(tmp_path, "nope", scope=SCOPE) is False
    assert len(memory_files.read_preferences(tmp_path)) == 1


def test_删空了也写文件(tmp_path: Path) -> None:
    """用户删掉最后一条的结果要留住，不能因为「没内容」就把文件恢复成原样。"""
    one = memory_files.add_preference(tmp_path, "taboo", "不要齿轮", scope=SCOPE)
    assert one is not None

    assert memory_files.delete_preference(tmp_path, one.id, scope=SCOPE) is True
    assert layout.preferences_path(tmp_path).is_file()
    assert memory_files.read_preferences(tmp_path) == []


def test_写坏了也不留半份(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """整份重写的写法只有配上原子替换才安全，否则中途出错就把共识截了一半。"""
    memory_files.write_preferences(tmp_path, [entry("preference", "冷色调")], scope=SCOPE)

    def boom(src: object, dst: object) -> None:
        raise OSError("盘满了")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        memory_files.write_preferences(tmp_path, [entry("preference", "暖色调")], scope=SCOPE)

    monkeypatch.undo()
    assert memory_files.read_preferences(tmp_path) == [entry("preference", "冷色调")]


# --------------------------------------------------------------------------- #
# 会话记忆
# --------------------------------------------------------------------------- #


def test_会话记忆往返(tmp_path: Path) -> None:
    memory = memory_files.AgentMemory(
        summary="前 4 轮压缩后的内容。",
        decisions=["双尾，红色辉光眼"],
        open_questions=["手指数还没定"],
        folded_turns=4,
    )

    memory_files.write_agent_memory(tmp_path, "spec_writer", memory, role="角色设计师")

    assert memory_files.read_agent_memory(tmp_path, "spec_writer") == memory


def test_折叠轮数写在头部(tmp_path: Path) -> None:
    """它是机器状态而不是给人看的共识，所以进 frontmatter；正文标题用角色名，人才认得出这是谁的。"""
    memory = memory_files.AgentMemory(summary="摘要", folded_turns=3)

    path = memory_files.write_agent_memory(tmp_path, "spec_writer", memory, role="角色设计师")

    text = path.read_text(encoding="utf-8")
    assert "folded_turns: 3" in text
    assert "# 角色设计师的会话记忆" in text


def test_没折叠过就不写这个键(tmp_path: Path) -> None:
    path = memory_files.write_agent_memory(
        tmp_path, "spec_writer", memory_files.AgentMemory(summary="摘要")
    )

    assert "folded_turns" not in path.read_text(encoding="utf-8")
    assert memory_files.read_agent_memory(tmp_path, "spec_writer").folded_turns == 0


def test_两个Agent各自一份(tmp_path: Path) -> None:
    """一场会话里各 Agent 记的不是同一件事，混在一份里下一个 Agent 会把别人的活儿当自己的。"""
    memory_files.write_agent_memory(
        tmp_path, "spec_writer", memory_files.AgentMemory(decisions=["双尾"])
    )
    memory_files.write_agent_memory(
        tmp_path, "game_designer", memory_files.AgentMemory(decisions=["赛博朋克"])
    )

    assert memory_files.read_agent_memory(tmp_path, "spec_writer").decisions == ["双尾"]
    assert memory_files.read_agent_memory(tmp_path, "game_designer").decisions == ["赛博朋克"]


# --------------------------------------------------------------------------- #
# 项目级提示词
# --------------------------------------------------------------------------- #


def test_附加指令往返(tmp_path: Path) -> None:
    prompt = memory_files.AgentPrompt(agent_code="spec_writer", content="本项目一律写单体角色。")

    memory_files.write_agent_prompt(tmp_path, prompt)

    assert memory_files.read_agent_prompt(tmp_path, "spec_writer") == prompt


def test_停用的附加指令读得回来(tmp_path: Path) -> None:
    """停用不是删除：列表里要看得见才改得回来，所以读回来带着 `enabled=False`。"""
    memory_files.write_agent_prompt(
        tmp_path,
        memory_files.AgentPrompt(agent_code="spec_writer", content="先别用这条", enabled=False),
    )

    got = memory_files.read_agent_prompt(tmp_path, "spec_writer")

    assert got is not None and got.enabled is False


def test_片段按文件名排(tmp_path: Path) -> None:
    memory_files.write_snippet(
        tmp_path, memory_files.Snippet(code="b_style", kind="style", content="湿滑金属")
    )
    memory_files.write_snippet(
        tmp_path,
        memory_files.Snippet(code="a_neg", kind="negative", content="低分辨率", slot="tail"),
    )

    got = memory_files.read_snippets(tmp_path)

    assert [one.code for one in got] == ["a_neg", "b_style"]
    assert got[0].slot == "tail"
