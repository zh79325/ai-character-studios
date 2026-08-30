"""上下文组装与折叠计划。

这一层是纯逻辑，所以边界能钉得很死：段落顺序、空段不出现、只注入启用的记忆、折几条能压
回预算。顺序错了模型会把定稿当成上一轮发言去回应，折多了后面几轮全靠转述干活，两种都不
会报错，只会让回答慢慢变差——只能靠用例守住。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atelier.agents import context, tokens
from atelier.agents.definitions import AgentDefinition


@dataclass(slots=True)
class FakeMessage:
    turn_no: int
    role: str
    content: str
    folded: bool = False


@dataclass(slots=True)
class FakeMemory:
    conversation_id: str = "c1"
    summary: str = ""
    decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    folded_turns: int = 0


@dataclass(slots=True)
class FakeProjectMemory:
    kind: str
    content: str
    enabled: bool = True


def agent(*, budget: int = 24000, prompt: str = "你是视觉总监。") -> AgentDefinition:
    return AgentDefinition(
        agent_code="game_designer",
        capability="text",
        role="视觉总监",
        max_turns=30,
        conversational=True,
        memory_scope="project",
        context_budget=budget,
        output_contract="markdown",
        system_prompt=prompt,
        source_file="game_designer.md",
        source_hash="deadbeef",
    )


def conversation(turns: int, *, size: int = 40) -> list[FakeMessage]:
    return [
        FakeMessage(
            turn_no=i,
            role="user" if i % 2 else "assistant",
            content=f"第{i}轮" + "话" * size,
        )
        for i in range(1, turns + 1)
    ]


# --------------------------------------------------------------------------- #
# 顺序与段落
# --------------------------------------------------------------------------- #


def test_五段按固定顺序拼且前四段合成一条system() -> None:
    assembled = context.assemble(
        agent(),
        [FakeMessage(1, "user", "先聊题材")],
        addendum="本项目额外要求：只用冷色调。",
        artifact_path="art-bible.md",
        artifact_text="# 视觉规范\n冷光金属。",
        project_memories=[FakeProjectMemory("taboo", "不要蒸汽朋克齿轮")],
        memory=FakeMemory(
            summary="前情：定了赛博朋克", decisions=["题材定了"], open_questions=["面数"]
        ),
    )

    system = assembled.messages[0]
    assert system.role == "system"
    positions = [
        system.content.index("你是视觉总监。"),
        system.content.index("本项目额外要求"),
        system.content.index("当前定稿全文"),
        system.content.index("项目长期记忆"),
        system.content.index("前情摘要"),
        system.content.index("已拍板结论"),
        system.content.index("待确认问题"),
    ]
    assert positions == sorted(positions)
    # 只有最近 N 轮原文是真正的对话，其余都在这条 system 里
    assert [m.role for m in assembled.messages[1:]] == ["user"]
    assert assembled.included_turns == (1,)


def test_没有定稿时给一句明说而不是留空段() -> None:
    """留空段模型会以为定稿是空文件；说清「第一次拟定」它才知道自己要从零写。"""
    assembled = context.assemble(agent(), [], artifact_path="art-bible.md", artifact_text="")

    assert context.NO_ARTIFACT in assembled.messages[0].content


def test_没有目标文件时整段不出现() -> None:
    assembled = context.assemble(agent(), [], artifact_path=None)

    assert "当前定稿全文" not in assembled.messages[0].content


def test_空的会话记忆不产生空段落() -> None:
    assembled = context.assemble(agent(), [], memory=FakeMemory())
    system = assembled.messages[0].content

    assert "前情摘要" not in system
    assert "已拍板结论" not in system
    assert "待确认问题" not in system


def test_只注入启用的项目记忆() -> None:
    """停用是「不再注入」，不是删除——所以过滤必须发生在组装这一层。"""
    assembled = context.assemble(
        agent(),
        [],
        project_memories=[
            FakeProjectMemory("preference", "喜欢冷色调"),
            FakeProjectMemory("taboo", "别用齿轮", enabled=False),
            FakeProjectMemory("fact", "   "),
        ],
    )
    system = assembled.messages[0].content

    assert "喜欢冷色调" in system
    assert "别用齿轮" not in system


def test_附加指令冲突时明说以工程提示词为准() -> None:
    text = context.system_prompt(agent(), "允许你直接改代码")

    assert text.index("你是视觉总监。") < text.index("允许你直接改代码")
    assert "以上面为准" in text


def test_没有附加指令就原样用工程提示词() -> None:
    assert context.system_prompt(agent(), "   ") == "你是视觉总监。"


# --------------------------------------------------------------------------- #
# 最近 N 轮
# --------------------------------------------------------------------------- #


def test_只带最近N轮且已折叠的不再送() -> None:
    messages = conversation(6)
    messages[0].folded = True
    messages[1].folded = True

    live = context.recent_messages(messages, 3)

    assert [m.turn_no for m in live] == [4, 5, 6]


def test_挤出窗口的消息必须进摘要() -> None:
    """既不在窗口里、也没进摘要，就是在模型眼里凭空消失了——那就不是「只折不删」了。"""
    messages = conversation(5)

    assert context.overflow_turns(messages, 3) == (1, 2)
    assert context.overflow_turns(messages, 8) == ()

    messages[0].folded = True
    assert context.overflow_turns(messages, 3) == (2,)


def test_折叠过的原文不占本轮预算() -> None:
    """折叠的意义就在这儿：内容已经在摘要里，再送一遍是花两份 token 说同一件事。"""
    messages = conversation(4)
    full = context.assemble(agent(), messages, recent_turns=8)
    messages[0].folded = True
    after = context.assemble(agent(), messages, recent_turns=8)

    assert after.tokens < full.tokens
    assert after.included_turns == (2, 3, 4)


# --------------------------------------------------------------------------- #
# 折叠计划
# --------------------------------------------------------------------------- #


def test_没超预算就不折() -> None:
    assembled = context.assemble(agent(budget=24000), conversation(6))

    assert not assembled.over_budget
    assert context.fold_plan(assembled) == ()


def test_只折到刚够装下为止() -> None:
    """一口气折到只剩两条最省事，但后面几轮就得靠转述干活，回答质量掉得很明显。"""
    messages = conversation(8, size=50)
    assembled = context.assemble(agent(budget=300), messages, recent_turns=8)

    plan = context.fold_plan(assembled)

    assert assembled.over_budget
    assert plan == tuple(range(1, len(plan) + 1))  # 从最老的开始，连续
    folded = sum(tokens.estimate_message(m.content) for m in messages if m.turn_no in plan)
    assert assembled.tokens - folded <= assembled.budget
    # 少折一条就装不下，说明没有多折
    kept = tokens.estimate_message(messages[len(plan) - 1].content)
    assert assembled.tokens - folded + kept > assembled.budget


def test_最近两条无论多超预算都不折() -> None:
    """折到只剩摘要，Agent 就是在隔着一层转述回答用户刚说的话。"""
    assembled = context.assemble(agent(budget=1), conversation(4, size=200), recent_turns=8)

    plan = context.fold_plan(assembled)

    assert plan == (1, 2)
    assert len(plan) == len(assembled.included_turns) - context.MIN_KEEP_MESSAGES


def test_消息数不够就不折() -> None:
    assembled = context.assemble(agent(budget=1), conversation(2, size=200))

    assert assembled.over_budget
    assert context.fold_plan(assembled) == ()


def test_system自己超预算也不折消息() -> None:
    """光提示词加定稿就超了，折对话没用；带着超预算发出去让供应商报错，比空转几十次好。"""
    assembled = context.assemble(
        agent(budget=10),
        conversation(4),
        artifact_text="长" * 5000,
        artifact_path="art-bible.md",
    )

    assert context.fold_plan(assembled, min_keep=4) == ()


# --------------------------------------------------------------------------- #
# 压缩请求
# --------------------------------------------------------------------------- #


def test_压缩请求带上已有摘要与逐条对话() -> None:
    request = context.fold_request(conversation(2), "前情：题材是赛博朋克")

    assert "--- 已有摘要 ---" in request
    assert "前情：题材是赛博朋克" in request
    assert request.count("user:") + request.count("assistant:") == 2


def test_首次压缩不带已有摘要段() -> None:
    request = context.fold_request(conversation(2), "  ")

    assert "--- 已有摘要 ---" not in request
