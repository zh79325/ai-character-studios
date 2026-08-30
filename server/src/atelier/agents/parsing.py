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

_DRAFT_RE = re.compile(
    r"^[ \t>]*\[草稿开始[:：]\s*(?P<path>[^\]\r\n]+?)\s*\]\s*$"
    r"(?P<body>.*?)"
    r"^[ \t>]*\[草稿结束\]\s*$",
    re.DOTALL | re.MULTILINE,
)

# 一个块从它的标记行开始，到下一个标记行或文本结束为止
_BLOCK_END_RE = re.compile(r"^[ \t>]*\[(?:草稿开始|草稿结束|对焦进度|项目记忆)", re.MULTILINE)

_KEY_RE = re.compile(r"^(?P<key>已定|待定|下一步)\s*[:：]\s*(?P<value>.*)$")
_MEMORY_RE = re.compile(rf"^(?P<kind>{'|'.join(MEMORY_KINDS)})\s*[:：]\s*(?P<value>.*)$", re.I)
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)、])\s*")
_FENCE_RE = re.compile(r"^\s*(?:```+|~~~+)")

_NONE_WORDS = frozenset({"暂无", "无", "none", "n/a", "-", "—"})


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
    """解析 `[项目记忆]`。只收认识的三类，别的行忽略。"""
    body = _block_body(text, MEMORY_MARKER)
    if body is None:
        return ()

    items: list[MemoryItem] = []
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
