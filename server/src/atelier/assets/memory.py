"""共识落盘：会话记忆、项目记忆与项目级提示词，都是项目目录里的 Markdown。

为什么不进库：`.atelier/project.db` 的定位是「随时可删可重建」，跨机器、跨人接手时不共享。
而这几份东西是跟用户谈出来的约定——尾巴要 2 条、不要粉色系、这个 Agent 在本项目额外守哪条
规矩——换个人接手必须看得到，所以它们跟着对象落在目录里、进 Git、可 review、可手改。

一个对象一份 `memory/`：项目的在项目根，角色的在角色目录下。会话记忆再按 Agent 分文件，
因为一场会话里多个 Agent 各自记的东西不一样（见 `layout.agent_memory_path`）。

格式一律是 YAML frontmatter + 固定小节，与 `atelier/prompts/agents/*.md` 同一套写法。写入
整份重写（临时文件 + `os.replace`），读取尽量容错：人手改过的行读不懂也照原文留着，不吞。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import yaml

from . import layout

_log = structlog.get_logger(__name__)

MEMORY_KINDS = ("preference", "taboo", "fact")
"""偏好文件里的小节名，与 `agents/parsing.MEMORY_KINDS` 同一套取值。"""

SCOPE_PROJECT = "project"
SCOPE_CHARACTER = "character"
"""frontmatter 里的 `scope`，只标是哪一档。是哪个角色由文件所在目录说明，写进正文只会在
角色改名搬目录之后变成假话。"""

SECTION_SUMMARY = "滚动摘要"
SECTION_DECISIONS = "已拍板"
SECTION_QUESTIONS = "待确认"

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", re.DOTALL)
_HEADING_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_ENTRY_RE = re.compile(r"^-\s+\[(?P<mark>[ xX])\]\s*(?P<content>.+?)\s*$")
_BULLET_RE = re.compile(r"^-\s+(?P<content>.+?)\s*$")

_PREFERENCES_INTRO = "聊出来的共识，可以直接改这份文件。`- [x]` 会注入提示词，`- [ ]` 只留档。"


def memory_hash(kind: str, content: str) -> str:
    """去重键：类别 + 归一化内容。

    同一条偏好在不同轮里措辞常差一个标点，按原文比对会攒出一堆近似重复，注入时全都占预算。
    这也是记忆条目对外的 id——内容即身份，不另发 uuid，文件被人手改后 id 跟着变才是对的。
    """
    normalized = re.sub(r"\s+", "", content).strip().lower()
    return hashlib.sha256(f"{kind}:{normalized}".encode()).hexdigest()


@dataclass(frozen=True)
class MemoryEntry:
    """一条项目记忆。字段与 `context.ProjectMemoryLike` 对齐，可直接进上下文组装。"""

    kind: str
    content: str
    enabled: bool = True

    @property
    def id(self) -> str:
        return memory_hash(self.kind, self.content)


@dataclass
class AgentMemory:
    """某个 Agent 在某个对象上的会话记忆。字段与 `context.MemoryLike` 对齐。"""

    summary: str = ""
    decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    folded_turns: int = 0

    def is_empty(self) -> bool:
        return not (self.summary or self.decisions or self.open_questions or self.folded_turns)


@dataclass(frozen=True)
class AgentPrompt:
    """项目级 Agent 附加指令：追加在工程提示词之后，不覆盖、不改写。"""

    agent_code: str
    content: str
    enabled: bool = True


@dataclass(frozen=True)
class Snippet:
    """项目自定义提示词片段：负向词、风格层等，与工程预设合并后使用。"""

    code: str
    kind: str
    content: str
    slot: str | None = None
    enabled: bool = True


# --------------------------------------------------------------------------- #
# 读写底座
# --------------------------------------------------------------------------- #


def _write_atomic(path: Path, content: str) -> None:
    """临时文件 + `os.replace`：宁可写不成，不能写一半。

    这些文件是共识的唯一真相，写坏了没有第二份可对。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _split(path: Path) -> tuple[dict[str, Any], str]:
    """拆成 frontmatter 与正文。没有头部就当整份都是正文，缺文件返回空。"""
    if not path.is_file():
        return {}, ""
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        return {}, raw
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        _log.warning("memory_frontmatter_unreadable", path=str(path))
        return {}, match.group(2)
    return (meta if isinstance(meta, dict) else {}), match.group(2)


def _sections(body: str) -> dict[str, list[str]]:
    """按 `## 标题` 切小节，返回标题到行列表。标题前的内容归到空串那一档。"""
    out: dict[str, list[str]] = {"": []}
    current = ""
    for line in body.splitlines():
        heading = _HEADING_RE.match(line)
        if heading is not None:
            current = heading.group("title")
            out.setdefault(current, [])
            continue
        out[current].append(line)
    return out


def _frontmatter(meta: dict[str, Any]) -> str:
    text = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{text}\n---\n"


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 项目记忆（preferences.md）
# --------------------------------------------------------------------------- #


def read_preferences(base_dir: Path) -> list[MemoryEntry]:
    """读这个对象的偏好与禁忌。文件不在就是还没聊出任何共识，返回空。

    小节名认不出来的（用户自己加了一节）整节跳过而不是硬塞进某一类：塞错类别会让「禁忌」
    被当成「偏好」注入，比丢掉更糟。同一条内容出现两次只留第一条。
    """
    path = layout.preferences_path(base_dir)
    _, body = _split(path)
    entries: list[MemoryEntry] = []
    seen: set[str] = set()
    for title, lines in _sections(body).items():
        if title not in MEMORY_KINDS:
            continue
        for line in lines:
            entry = _parse_entry(title, line)
            if entry is None:
                continue
            if entry.id in seen:
                continue
            seen.add(entry.id)
            entries.append(entry)
    return entries


def _parse_entry(kind: str, line: str) -> MemoryEntry | None:
    """一行变一条。带勾选框的按勾选定启用，光秃秃的列表项当启用（人手写的默认生效）。"""
    marked = _ENTRY_RE.match(line)
    if marked is not None:
        return MemoryEntry(
            kind=kind,
            content=marked.group("content"),
            enabled=marked.group("mark").lower() == "x",
        )
    plain = _BULLET_RE.match(line)
    if plain is not None:
        return MemoryEntry(kind=kind, content=plain.group("content"))
    return None


def write_preferences(base_dir: Path, entries: list[MemoryEntry], *, scope: str) -> Path:
    """整份重写偏好文件。空列表也写：用户手动删空了要留住这个结果。"""
    path = layout.preferences_path(base_dir)
    meta = {"scope": scope, "updated_at": _now()}
    parts = [_frontmatter(meta), f"\n# 用户偏好与禁忌\n\n{_PREFERENCES_INTRO}\n"]
    for kind in MEMORY_KINDS:
        mine = [one for one in entries if one.kind == kind]
        if not mine:
            continue
        parts.append(f"\n## {kind}\n\n")
        for one in mine:
            mark = "x" if one.enabled else " "
            parts.append(f"- [{mark}] {one.content}\n")
    _write_atomic(path, "".join(parts))
    return path


def add_preference(
    base_dir: Path, kind: str, content: str, *, scope: str, taken: set[str] | None = None
) -> MemoryEntry | None:
    """追写一条，已经有一样的返回 None。

    `taken` 是别处已占用的去重键（角色级要让着项目级：项目级已经有同一句时不再写副本，
    否则两条一模一样的记忆同时注入，用户在设置页关掉其中一条会发现它依旧生效）。
    """
    entry = MemoryEntry(kind=kind, content=content)
    entries = read_preferences(base_dir)
    existing = {one.id for one in entries} | (taken or set())
    if entry.id in existing:
        return None
    entries.append(entry)
    write_preferences(base_dir, entries, scope=scope)
    return entry


def update_preference(
    base_dir: Path,
    entry_id: str,
    *,
    scope: str,
    content: str | None = None,
    enabled: bool | None = None,
) -> MemoryEntry | None:
    """改一条，找不到返回 None。

    改内容会换一个 id（id 就是内容哈希），所以把新那条返回去——调用方得拿新 id 重新寻址。
    改完撞上已有的同一句就只留改过的那条，不报错：用户想要的就是两条并成一条。
    """
    entries = read_preferences(base_dir)
    hit = next((one for one in entries if one.id == entry_id), None)
    if hit is None:
        return None
    updated = MemoryEntry(
        kind=hit.kind,
        content=hit.content if content is None else content,
        enabled=hit.enabled if enabled is None else enabled,
    )
    kept = [
        updated if one.id == entry_id else one
        for one in entries
        if one.id == entry_id or one.id != updated.id
    ]
    write_preferences(base_dir, kept, scope=scope)
    return updated


def delete_preference(base_dir: Path, entry_id: str, *, scope: str) -> bool:
    """删一条，真删掉了返回真。"""
    entries = read_preferences(base_dir)
    kept = [one for one in entries if one.id != entry_id]
    if len(kept) == len(entries):
        return False
    write_preferences(base_dir, kept, scope=scope)
    return True


# --------------------------------------------------------------------------- #
# 会话记忆（memory/agents/{agent_code}.md）
# --------------------------------------------------------------------------- #


def read_agent_memory(base_dir: Path, agent_code: str) -> AgentMemory:
    """读某个 Agent 在这个对象上的记忆。文件不在返回空壳，不建文件。"""
    path = layout.agent_memory_path(base_dir, agent_code)
    meta, body = _split(path)
    sections = _sections(body)
    folded = meta.get("folded_turns")
    return AgentMemory(
        summary="\n".join(sections.get(SECTION_SUMMARY, [])).strip(),
        decisions=_bullets(sections.get(SECTION_DECISIONS, [])),
        open_questions=_bullets(sections.get(SECTION_QUESTIONS, [])),
        folded_turns=folded if isinstance(folded, int) and folded > 0 else 0,
    )


def _bullets(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        match = _BULLET_RE.match(line)
        if match is not None:
            out.append(match.group("content"))
    return out


def write_agent_memory(
    base_dir: Path, agent_code: str, memory: AgentMemory, *, role: str = ""
) -> Path:
    path = layout.agent_memory_path(base_dir, agent_code)
    meta: dict[str, Any] = {"agent": agent_code, "updated_at": _now()}
    if memory.folded_turns:
        meta["folded_turns"] = memory.folded_turns
    who = role or agent_code
    parts = [_frontmatter(meta), f"\n# {who}的会话记忆\n"]
    if memory.summary:
        parts.append(f"\n## {SECTION_SUMMARY}\n\n{memory.summary}\n")
    for title, items in (
        (SECTION_DECISIONS, memory.decisions),
        (SECTION_QUESTIONS, memory.open_questions),
    ):
        if not items:
            continue
        parts.append(f"\n## {title}\n\n")
        parts.extend(f"- {one}\n" for one in items)
    _write_atomic(path, "".join(parts))
    return path


# --------------------------------------------------------------------------- #
# 项目级提示词（prompts/）
# --------------------------------------------------------------------------- #


def read_agent_prompt(project_dir: Path, agent_code: str) -> AgentPrompt | None:
    """项目级附加指令。文件不在或正文是空的返回 None；停用的照样读回来，用不用由调用方定。"""
    path = layout.agent_prompt_path(project_dir, agent_code)
    meta, body = _split(path)
    content = body.strip()
    if not content:
        return None
    return AgentPrompt(
        agent_code=agent_code, content=content, enabled=meta.get("enabled", True) is not False
    )


def write_agent_prompt(project_dir: Path, prompt: AgentPrompt) -> Path:
    path = layout.agent_prompt_path(project_dir, prompt.agent_code)
    meta = {"enabled": prompt.enabled, "updated_at": _now()}
    _write_atomic(path, f"{_frontmatter(meta)}\n{prompt.content.strip()}\n")
    return path


def read_snippets(project_dir: Path) -> list[Snippet]:
    """项目自定义片段，按文件名排序。缺 `kind` 的按文件名当 code、kind 记空串。"""
    directory = layout.snippet_dir(project_dir)
    if not directory.is_dir():
        return []
    out: list[Snippet] = []
    for path in sorted(directory.glob("*.md")):
        meta, body = _split(path)
        content = body.strip()
        if not content:
            continue
        slot = meta.get("slot")
        out.append(
            Snippet(
                code=path.stem,
                kind=str(meta.get("kind") or ""),
                content=content,
                slot=str(slot) if slot else None,
                enabled=meta.get("enabled", True) is not False,
            )
        )
    return out


def write_snippet(project_dir: Path, snippet: Snippet) -> Path:
    path = layout.snippet_path(project_dir, snippet.code)
    meta: dict[str, Any] = {"kind": snippet.kind, "enabled": snippet.enabled}
    if snippet.slot:
        meta["slot"] = snippet.slot
    meta["updated_at"] = _now()
    _write_atomic(path, f"{_frontmatter(meta)}\n{snippet.content.strip()}\n")
    return path


__all__ = [
    "MEMORY_KINDS",
    "SCOPE_CHARACTER",
    "SCOPE_PROJECT",
    "AgentMemory",
    "AgentPrompt",
    "MemoryEntry",
    "Snippet",
    "add_preference",
    "delete_preference",
    "memory_hash",
    "read_agent_memory",
    "read_agent_prompt",
    "read_preferences",
    "read_snippets",
    "update_preference",
    "write_agent_memory",
    "write_agent_prompt",
    "write_preferences",
    "write_snippet",
]
