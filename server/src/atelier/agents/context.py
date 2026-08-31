"""每轮上下文组装。

顺序是定死的，因为它表达的是优先级——越靠前的越不可能被折叠掉：

1. Agent 提示词（工程级 system + 项目级附加指令）
2. 项目美术规范全文（`art-bible.md`，只在角色这类子目标的会话里带——立项会话的定稿就是它）
3. 目标当前定稿全文（`art-bible.md` 或 `{角色名}角色设定.md`，没有则跳过）
   外加项目配置现状（`project.json` 里 Agent 能改的那几段），立项会话要照它做增量调整
4. 项目长期记忆（用户偏好、口味、明确禁忌）
5. 会话记忆：滚动摘要 + 已拍板结论 + 待确认问题
6. 最近 N 轮原文

前五项拼成一条 system 消息，只有第 6 项是真正的对话。这样做的理由：定稿与记忆是「事实」
而非「谁说过的话」，混进 user/assistant 序列里模型会把它当成上一轮发言去回应。

总量卡预算：模型窗口已知时按 `effective_budget` 取窗口的一个比例，未知才回落 Agent 自己写的
`context_budget`。超了不截断消息——截断会悄悄丢掉用户说过的话；改由调用方把最老的未折叠消息
交给同一 Agent 压缩进摘要（`fold_plan`），原文留在库里仍可展开回看。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from atelier.agents import tokens
from atelier.agents.definitions import AgentDefinition

MIN_KEEP_MESSAGES = 2
"""无论多超预算，最近两条（用户这轮的话与上一轮回答）不折叠。

折到只剩摘要就等于让 Agent 隔着一层转述回答用户刚说的话，答非所问的概率极高；这两条
真的还塞不进去，那是模型窗口配小了，该报错而不是继续折。
"""

SECTION_ART_BIBLE = "## 项目美术规范（{path}，全局约束，与它冲突的设计不算过关）"
SECTION_ARTIFACT = "## 当前定稿全文（{path}）"
SECTION_PROJECT_CONFIG = "## 项目配置现状（{path}，只有这几个键归你改）"
SECTION_PROJECT_MEMORY = "## 项目长期记忆（用户明确表达过的，务必遵守）"
SECTION_CONV_SUMMARY = "## 本次会话已压缩的前情摘要"
SECTION_DECISIONS = "## 已拍板结论"
SECTION_OPEN = "## 待确认问题"

NO_ARTIFACT = "（尚无定稿，这是第一次拟定）"

FOLD_INSTRUCTION = """请把下面这段对话记录压缩成不超过 {limit} 字的前情摘要。

要求：
- 只保留后续对焦还用得上的信息：已拍板的结论、用户明确的偏好与禁忌、尚未回答的问题。
- 逐条陈述，不要评论、不要复述提问的措辞、不要加开场与结尾。
- 已有摘要在最前面，把新内容合并进去，同一件事只留最新结论，不要写成两条互相矛盾的。
- 直接输出摘要正文，不要加标题、不要用代码块包起来。

{existing}
--- 待压缩的对话记录 ---
{transcript}
"""


class MessageLike(Protocol):
    """`project_models.Message` 的读取口。

    用协议而不是直接依赖 ORM：组装是纯逻辑，测试里拿几个 dataclass 就能覆盖各种预算边界，
    不必为了验证「折到第几条」去建一个项目库。
    """

    turn_no: int
    role: str
    content: str
    folded: bool


@dataclass(slots=True)
class Ask:
    """单次调用的那一条用户消息。

    评审、提示词翻译这类一问一答的 Agent 没有对话历史，但上下文的拼装顺序（提示词 → 定稿
    → 项目记忆）跟会话完全一样，没必要另写一套拼法。
    """

    content: str
    turn_no: int = 1
    role: str = "user"
    folded: bool = False


class MemoryLike(Protocol):
    conversation_id: str
    summary: str
    open_questions: list[str]
    decisions: list[str]
    folded_turns: int


class ProjectMemoryLike(Protocol):
    kind: str
    content: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """送给模型的一条消息。与 provider 的 wire 格式一一对应。"""

    role: str
    content: str

    def as_payload(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class Assembled:
    """一次组装结果。"""

    messages: tuple[ChatMessage, ...]
    tokens: int
    budget: int
    system_tokens: int
    included_turns: tuple[int, ...]
    """本次带上原文的消息 turn_no，写进 route_logs 便于复盘「当时模型看见了什么」。"""

    @property
    def over_budget(self) -> bool:
        return self.tokens > self.budget

    def payload(self) -> list[dict[str, str]]:
        return [m.as_payload() for m in self.messages]


def system_prompt(agent: AgentDefinition, addendum: str | None) -> str:
    """工程提示词 + 项目级附加指令。

    附加指令只能追加、不能覆盖：工程提示词是代码资产，项目想改口径也只是在后面补充，
    冲突时以工程提示词为准（这句话本身也写进拼接文本，免得模型自己判断）。
    """
    if not addendum or not addendum.strip():
        return agent.system_prompt
    return (
        f"{agent.system_prompt}\n\n"
        "---\n\n"
        "## 本项目的附加指令\n\n"
        "以下是本项目为你补充的要求，与上面的职责冲突时以上面为准。\n\n"
        f"{addendum.strip()}\n"
    )


def _bullets(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def effective_budget(agent: AgentDefinition, window: int | None, ratio: float) -> int:
    """本轮的上下文预算。

    Agent frontmatter 里的 `context_budget` 是写提示词时拍的保守值，当年的模型窗口就那么大；
    现在一个 1M 窗口的模型拿 24k 的预算干活，明明装得下却每几轮就去折一次，每折一次就多
    一层转述。所以窗口已知就按窗口算，留 `1 - ratio` 给输出与估算误差。

    窗口比 Agent 预算还小时也听窗口的（比如只配了个 8k 的小模型）：窗口是硬上限，拍得比它大
    只会把包发成超限。ratio 不超 1，所以直接按比例算就已经包含这一层约束。
    """
    if window is None or window <= 0:
        return agent.context_budget
    return max(1, int(window * ratio))


def context_block(
    *,
    artifact_path: str | None,
    artifact_text: str | None,
    art_bible_path: str | None = None,
    art_bible_text: str | None = None,
    config_path: str | None = None,
    config_text: str | None = None,
    project_memories: Sequence[ProjectMemoryLike] = (),
    memory: MemoryLike | None = None,
) -> str:
    """第 2-5 项拼成的上下文块，空的段落整段不出现。"""
    parts: list[str] = []

    # 美术规范排在定稿前面：它是约束，得先看见约束再看当前稿子，否则容易顺着旧稿的口径接着写
    if art_bible_path is not None and (art_bible_text or "").strip():
        section = SECTION_ART_BIBLE.format(path=art_bible_path)
        parts.append(f"{section}\n\n{(art_bible_text or '').strip()}")

    if artifact_path is not None:
        body = (artifact_text or "").strip() or NO_ARTIFACT
        parts.append(f"{SECTION_ARTIFACT.format(path=artifact_path)}\n\n{body}")

    # 配置现状只给能改的那几段：Agent 看得见现值才谈得上「只改这一处」，看得见键名才不会
    # 自己发明一个平台不认识的键——那种建议合并时会被静默丢掉，用户以为改了其实没改。
    if config_path is not None and (config_text or "").strip():
        section = SECTION_PROJECT_CONFIG.format(path=config_path)
        parts.append(f"{section}\n\n```json\n{(config_text or '').strip()}\n```")

    enabled = [m for m in project_memories if m.enabled and m.content.strip()]
    if enabled:
        lines = _bullets([f"[{m.kind}] {m.content.strip()}" for m in enabled])
        parts.append(f"{SECTION_PROJECT_MEMORY}\n\n{lines}")

    if memory is not None:
        if memory.summary.strip():
            parts.append(f"{SECTION_CONV_SUMMARY}\n\n{memory.summary.strip()}")
        if memory.decisions:
            parts.append(f"{SECTION_DECISIONS}\n\n{_bullets(memory.decisions)}")
        if memory.open_questions:
            parts.append(f"{SECTION_OPEN}\n\n{_bullets(memory.open_questions)}")

    return "\n\n".join(parts)


def recent_messages(messages: Sequence[MessageLike], recent_turns: int) -> tuple[MessageLike, ...]:
    """参与本轮的原文：未折叠的最后 N 条，按 turn_no 升序。

    已折叠的不再送——它们的内容已经在摘要里，再送一遍等于花两份 token 说同一件事。
    """
    live = sorted((m for m in messages if not m.folded), key=lambda m: m.turn_no)
    return tuple(live[-recent_turns:]) if recent_turns > 0 else ()


def overflow_turns(messages: Sequence[MessageLike], recent_turns: int) -> tuple[int, ...]:
    """未折叠、却已经被挤出最近 N 轮窗口的消息。

    这些消息既不进上下文、也还没进摘要，等于在模型眼里凭空消失了——违背「只折不删」。所以
    它们不管预算够不够都要先折进摘要，只是折的时机由调用方跟超预算那一批合成一次调用。
    """
    live = sorted((m for m in messages if not m.folded), key=lambda m: m.turn_no)
    if recent_turns <= 0:
        return tuple(m.turn_no for m in live)
    return tuple(m.turn_no for m in live[:-recent_turns]) if len(live) > recent_turns else ()


def assemble(
    agent: AgentDefinition,
    messages: Sequence[MessageLike],
    *,
    addendum: str | None = None,
    artifact_path: str | None = None,
    artifact_text: str | None = None,
    art_bible_path: str | None = None,
    art_bible_text: str | None = None,
    config_path: str | None = None,
    config_text: str | None = None,
    project_memories: Sequence[ProjectMemoryLike] = (),
    memory: MemoryLike | None = None,
    recent_turns: int = 20,
    budget: int | None = None,
) -> Assembled:
    """按固定顺序拼出这一轮要发的消息。"""
    system = system_prompt(agent, addendum)
    block = context_block(
        artifact_path=artifact_path,
        artifact_text=artifact_text,
        art_bible_path=art_bible_path,
        art_bible_text=art_bible_text,
        config_path=config_path,
        config_text=config_text,
        project_memories=project_memories,
        memory=memory,
    )
    if block:
        system = f"{system}\n\n---\n\n{block}\n"

    live = recent_messages(messages, recent_turns)
    chat = tuple(ChatMessage(role=m.role, content=m.content) for m in live)

    system_tokens = tokens.estimate_message(system)
    total = system_tokens + sum(tokens.estimate_message(m.content) for m in chat)

    return Assembled(
        messages=(ChatMessage(role="system", content=system), *chat),
        tokens=total,
        budget=budget if budget is not None else agent.context_budget,
        system_tokens=system_tokens,
        included_turns=tuple(m.turn_no for m in live),
    )


def fold_plan(assembled: Assembled, *, min_keep: int = MIN_KEEP_MESSAGES) -> tuple[int, ...]:
    """要把哪几条最老的原文折进摘要，返回它们的 turn_no（升序）。

    一次只算「至少折几条能压回预算内」，而不是一口气折到只剩 min_keep——上下文是每轮重
    组的，折多了后面几轮就得靠转述干活，回答质量掉得很明显。
    """
    if not assembled.over_budget or len(assembled.included_turns) <= min_keep:
        return ()

    running = assembled.tokens
    folded: list[int] = []
    body = list(zip(assembled.included_turns, assembled.messages[1:], strict=True))
    for turn_no, message in body[: len(body) - min_keep]:
        running -= tokens.estimate_message(message.content)
        folded.append(turn_no)
        if running <= assembled.budget:
            break
    return tuple(folded)


def fold_request(
    transcript: Sequence[MessageLike], existing_summary: str, *, limit: int = 1500
) -> str:
    """压缩请求的正文。

    交给同一个 Agent 而不是另找一个「摘要模型」：它认识这场对话的口径与术语，也已经在
    这个会话的绑定候选上，换模型既多一次冷启动、又可能把结论理解偏。
    """
    lines = [f"{m.role}: {m.content}" for m in transcript]
    existing = f"--- 已有摘要 ---\n{existing_summary.strip()}\n" if existing_summary.strip() else ""
    return FOLD_INSTRUCTION.format(limit=limit, existing=existing, transcript="\n\n".join(lines))
