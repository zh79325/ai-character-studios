"""解析会话型 Agent 每轮输出里的结构块。

提示词里跟 Agent 约好三个块：`[对焦进度]`、`[草稿开始: 路径]…[草稿结束]`、`[项目记忆]`。
平台靠它们把自由对话变成可落库的东西——进度进 `conversation_memory`，草稿进
`artifact_drafts`，记忆在确认沉淀时进 `project_memory`。

宽进严出：模型会把块包在 ``` 里、会用半角冒号、会把模板占位符 `<…>` 原样吐回来、会写
「暂无」。这些一律容错或跳过，但绝不猜测没写的内容——解析不出草稿就是这轮没有草稿，
让用户继续聊，而不是拿半截文本去覆盖定稿。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MEMORY_KINDS = ("preference", "taboo", "fact")

PROGRESS_MARKER = "[对焦进度]"
MEMORY_MARKER = "[项目记忆]"
CHARACTER_MEMORY_MARKER = "[角色记忆]"
"""角色型 Agent 的记忆块标记。

跟 `[项目记忆]` 解析成同一种东西：记忆归项目还是归角色，由**这场会话在跟谁对焦**决定，不由
模型选的标记决定。让模型自己挑作用域的话，它在角色会话里写一句 `[项目记忆]` 就把一条只对
这个角色成立的要求塞给了全项目，而这条会一路跟到别的角色的提示词里。
"""

VERDICTS = ("APPROVE", "CONCERNS", "REJECT")
SPEC_CHECK = "SPEC-CHECK"
VIEW_CHECK = "VIEW-CHECK"
CONSTRAINTS_SECTION = "硬性约束清单"

_DRAFT_RE = re.compile(
    r"^[ \t>]*\[草稿开始[:：]\s*(?P<path>[^\]\r\n]+?)\s*\]\s*$"
    r"(?P<body>.*?)"
    r"^[ \t>]*\[草稿结束\]\s*$",
    re.DOTALL | re.MULTILINE,
)

# 一个块从它的标记行开始，到下一个标记行或文本结束为止
_BLOCK_END_RE = re.compile(
    r"^[ \t>]*\[(?:草稿开始|草稿结束|对焦进度|项目记忆|角色记忆)", re.MULTILINE
)

_KEY_RE = re.compile(r"^(?P<key>已定|待定|下一步)\s*[:：]\s*(?P<value>.*)$")
_MEMORY_RE = re.compile(rf"^(?P<kind>{'|'.join(MEMORY_KINDS)})\s*[:：]\s*(?P<value>.*)$", re.I)
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)、])\s*")
_FENCE_RE = re.compile(r"^\s*(?:```+|~~~+)")

_NONE_WORDS = frozenset({"暂无", "无", "none", "n/a", "-", "—"})

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(?P<title>.+?)\s*#*\s*$")
_CONSTRAINT_RE = re.compile(r"^(?P<item>[^=:：]{1,60}?)\s*(?:=|→|:|：)\s*(?P<value>.+)$")


def is_placeholder(text: str) -> bool:
    """模板占位符与「暂无」不算内容。

    提示词里写的是 `<一行一条，或「暂无」>`，模型照抄回来的情况很常见；把它当结论存进
    记忆，用户下次就会看到 Agent 一本正经地复述一句尖括号。
    """
    stripped = text.strip().strip("`").strip()
    if not stripped or stripped.lower() in _NONE_WORDS:
        return True
    return stripped.startswith("<") and stripped.endswith(">")


@dataclass(frozen=True, slots=True)
class DraftBlock:
    """一份完整产物草稿。`target_path` 是 Agent 声明的落盘位置，还要由调用方校验。"""

    target_path: str
    content: str


@dataclass(frozen=True, slots=True)
class MemoryItem:
    kind: str
    content: str


@dataclass(frozen=True, slots=True)
class Progress:
    """这一轮的对焦进度。三项都可能为空——刚开场时本来就还没有结论。"""

    decisions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    next_step: str | None = None

    def is_empty(self) -> bool:
        return not (self.decisions or self.open_questions or self.next_step)


@dataclass(frozen=True, slots=True)
class TurnOutput:
    """一轮助手输出的解析结果，原文始终原样保留。"""

    text: str
    progress: Progress | None = None
    drafts: tuple[DraftBlock, ...] = ()
    memories: tuple[MemoryItem, ...] = ()

    @property
    def has_draft(self) -> bool:
        return bool(self.drafts)


def _strip_fences(body: str) -> str:
    """去掉草稿正文首尾可能包着的代码围栏。

    模型常把整个块塞进 ``` 里好让 Markdown 显示得规整，但围栏不是文件内容——把它写进
    art-bible.md 会让下游解析第 6 节时多出两行噪声。只削首尾各一层，正文里的围栏
    （比如设定文档中引用的代码）原样留着。
    """
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _FENCE_RE.match(lines[0]) and len(lines) > 1 and _FENCE_RE.match(lines[-1]):
        lines = lines[1:-1]
    return "\n".join(lines).strip("\n")


def parse_drafts(text: str) -> tuple[DraftBlock, ...]:
    """抽出全部草稿块。一轮里可以有多份（art-bible.md 与 project.json 就是一对）。"""
    drafts: list[DraftBlock] = []
    for match in _DRAFT_RE.finditer(text):
        path = match.group("path").strip().strip("`")
        content = _strip_fences(match.group("body"))
        if not path or is_placeholder(path) or not content:
            continue
        drafts.append(DraftBlock(target_path=path, content=content + "\n"))
    return tuple(drafts)


def _block_body(text: str, marker: str) -> str | None:
    """取标记行之后、下一个标记行之前的内容。"""
    start = text.find(marker)
    if start < 0:
        return None
    after = start + len(marker)
    nxt = _BLOCK_END_RE.search(text, after)
    return text[after : nxt.start()] if nxt else text[after:]


def _items(raw: str) -> list[str]:
    """把「一行一条」的值切成条目，顺带去掉列表符号与占位符。"""
    items: list[str] = []
    for line in raw.splitlines():
        item = _BULLET_RE.sub("", line).strip()
        if item and not is_placeholder(item):
            items.append(item)
    return items


def parse_progress(text: str) -> Progress | None:
    """解析 `[对焦进度]`。没有这个块返回 None，与「有块但三项都空」区分开。"""
    body = _block_body(text, PROGRESS_MARKER)
    if body is None:
        return None

    buckets: dict[str, list[str]] = {"已定": [], "待定": [], "下一步": []}
    current: str | None = None
    for line in body.splitlines():
        match = _KEY_RE.match(line.strip())
        if match:
            current = match.group("key")
            buckets[current].extend(_items(match.group("value")))
            continue
        if current is not None:
            buckets[current].extend(_items(line))

    next_step = "；".join(buckets["下一步"]) or None
    return Progress(
        decisions=tuple(buckets["已定"]),
        open_questions=tuple(buckets["待定"]),
        next_step=next_step,
    )


def parse_memories(text: str) -> tuple[MemoryItem, ...]:
    """解析 `[项目记忆]` 与 `[角色记忆]`。只收认识的三类，别的行忽略。

    两个标记合起来收：作用域由会话的对焦对象定（见 `CHARACTER_MEMORY_MARKER`），这里只管
    把条目捞出来。两个块都写了就都收，顺序按它们在文里出现的先后。
    """
    bodies = [
        body
        for marker in (MEMORY_MARKER, CHARACTER_MEMORY_MARKER)
        if (body := _block_body(text, marker)) is not None
    ]
    if not bodies:
        return ()

    items: list[MemoryItem] = []
    for body in bodies:
        kind: str | None = None
        for line in body.splitlines():
            stripped = _BULLET_RE.sub("", line).strip()
            match = _MEMORY_RE.match(stripped)
            if match:
                kind = match.group("kind").lower()
                value = match.group("value").strip()
                if value and not is_placeholder(value):
                    items.append(MemoryItem(kind=kind, content=value))
                continue
            # 同一类下的后续行沿用上一个 kind：模型常把三条偏好分三行写在 preference 下面
            if kind is not None and stripped and not is_placeholder(stripped):
                items.append(MemoryItem(kind=kind, content=stripped))
    return tuple(items)


def parse_turn(text: str) -> TurnOutput:
    """解析一轮助手输出。原文原样带回，前端展示的仍是 Agent 说的话。"""
    return TurnOutput(
        text=text,
        progress=parse_progress(text),
        drafts=parse_drafts(text),
        memories=parse_memories(text),
    )


# --------------------------------------------------------------------------- #
# 裁决型 Agent
# --------------------------------------------------------------------------- #


class VerdictError(ValueError):
    """裁决回答的首行不是约定的 token。"""


@dataclass(frozen=True, slots=True)
class Constraint:
    """一条可逐张图对着回答「符合 / 不符合」的硬约束。"""

    item: str
    value: str


@dataclass(frozen=True, slots=True)
class Verdict:
    """一次自动裁决。`text` 是全文，进 `task_events` 供回溯。"""

    token: str
    decision: str
    sections: dict[str, tuple[str, ...]]
    constraints: tuple[Constraint, ...] = ()
    text: str = ""

    @property
    def approved(self) -> bool:
        """只表示审校没发现问题，放不放行仍然是人工门禁的事。"""
        return self.decision == "APPROVE"

    @property
    def rejected(self) -> bool:
        return self.decision == "REJECT"


def _verdict_line(text: str, token: str) -> str:
    """拿首行。只跳前面的空行与围栏，不往正文里找。

    提示词里跟它说好了「不得把 token 藏在段落里」。要是这里宽容到满文搜，它写一段「若修好则
    APPROVE」也会被当成通过，那是把条件句当结论读。
    """
    for line in text.splitlines():
        stripped = line.strip().strip("*").strip()
        if not stripped or _FENCE_RE.match(stripped):
            continue
        return stripped
    raise VerdictError(f"{token} 的回答是空的")


def parse_verdict(text: str, token: str = SPEC_CHECK) -> Verdict:
    """解析裁决型 Agent 的回答：首行 `{token}: APPROVE|CONCERNS|REJECT`，理由在下面分节写。

    首行不合规就报错而不是当成 REJECT：看不出裁决是模型没按契约说话，该重试或转人工；默认
    成 REJECT 会把一次格式事故变成一次对设定的否定，用户拿到的是一份没有理由的驳回。
    """
    first = _verdict_line(text, token)
    match = re.match(rf"^{re.escape(token)}\s*[:：]\s*(?P<decision>[A-Za-z]+)", first, re.I)
    if match is None:
        raise VerdictError(f"首行不是 {token} 裁决，而是 {first[:60]!r}")
    decision = match.group("decision").upper()
    if decision not in VERDICTS:
        raise VerdictError(f"{token} 的裁决 {decision!r} 不在 {'/'.join(VERDICTS)} 里")

    sections = _sections(text)
    return Verdict(
        token=token,
        decision=decision,
        sections=sections,
        constraints=_constraints(sections),
        text=text,
    )


def _sections(text: str) -> dict[str, tuple[str, ...]]:
    """把 `### 标题` 下的条目按标题归堆。标题原文当键：模型会改措辞，匹配由谁用谁包含。"""
    buckets: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            current = heading.group("title").strip().strip("*").strip()
            buckets.setdefault(current, [])
            continue
        if current is None:
            continue
        item = _BULLET_RE.sub("", line).strip().strip("*").strip()
        if item and not is_placeholder(item):
            buckets[current].append(item)
    return {title: tuple(items) for title, items in buckets.items()}


def _constraints(sections: dict[str, tuple[str, ...]]) -> tuple[Constraint, ...]:
    """从硬性约束那一节抽出 `项 = 值`。

    拆成两截而不是存整行：这份清单进 `meta.json` 后要被后续每张图逐条比对，比对得按项
    对得上号。拆不出项名的行直接丢：拆不出就是不可逐条判定，存下去也没人能用。
    """
    title = next((key for key in sections if CONSTRAINTS_SECTION in key), None)
    if title is None:
        return ()
    picked: list[Constraint] = []
    for line in sections[title]:
        match = _CONSTRAINT_RE.match(line)
        if match is None:
            continue
        item = match.group("item").strip().strip("`").strip()
        value = match.group("value").strip()
        if item and value:
            picked.append(Constraint(item=item, value=value))
    return tuple(picked)
