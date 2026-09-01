"""Agent 定义文件解析：YAML frontmatter + Markdown 正文，带校验。

工程级提示词是代码资产，只住在 atelier/prompts/agents/*.md，不入库、不得硬编码进
Python，也不得由 UI 修改；本模块只负责读、校验与缓存。项目级的附加指令在项目目录的
`prompts/agents/{agent_code}.md` 里，组装上下文时追加在工程提示词之后。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from atelier.settings import get_settings

CAPABILITIES = {"text", "t2i", "i2i", "vision", "model3d", "t2v", "i2v"}
MEMORY_SCOPES = {"project", "character", "none"}
OUTPUT_CONTRACTS = {"markdown_spec", "asset_spec", "verdict", "image", "json"}
ROLE_TYPES = {"director", "specialist", "executor"}
TARGET_KINDS = {"project", "character"}

REQUIRED_KEYS = (
    "agent_code",
    "capability",
    "role",
    "role_type",
    "focusable",
    "aliases",
    "target_kinds",
    "stages",
    "max_turns",
    "conversational",
    "memory_scope",
    "context_budget",
    "output_contract",
)

# 每份提示词必含的固定章节
REQUIRED_SECTIONS = ("### 职责", "### 输出格式", "### 绝不可做")

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", re.DOTALL)


class AgentDefinitionError(ValueError):
    """Agent 定义文件不合规。"""


@dataclass(slots=True)
class AgentDefinition:
    agent_code: str
    capability: str
    role: str
    max_turns: int
    conversational: bool
    memory_scope: str
    context_budget: int
    output_contract: str
    system_prompt: str
    source_file: str
    source_hash: str
    role_type: str = "specialist"
    focusable: bool = False
    aliases: tuple[str, ...] = ()
    target_kinds: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()
    max_output_tokens: int | None = None
    allow_tools: list[str] = field(default_factory=list)


def parse_agent_file(path: Path) -> AgentDefinition:
    """解析单个 Agent 定义文件，任一校验不过即抛 AgentDefinitionError。"""
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        raise AgentDefinitionError(f"{path.name}: 缺少 YAML frontmatter（--- 包裹的头部）")

    meta_text, body = match.group(1), match.group(2).strip()
    meta: Any = yaml.safe_load(meta_text)
    if not isinstance(meta, dict):
        raise AgentDefinitionError(f"{path.name}: frontmatter 不是键值映射")

    missing = [k for k in REQUIRED_KEYS if k not in meta]
    if missing:
        raise AgentDefinitionError(f"{path.name}: frontmatter 缺字段 {missing}")

    if meta["agent_code"] != path.stem:
        raise AgentDefinitionError(f"{path.name}: agent_code={meta['agent_code']!r} 与文件名不一致")
    if meta["capability"] not in CAPABILITIES:
        raise AgentDefinitionError(f"{path.name}: capability={meta['capability']!r} 非法")
    if meta["role_type"] not in ROLE_TYPES:
        raise AgentDefinitionError(f"{path.name}: role_type={meta['role_type']!r} 非法")
    if meta["memory_scope"] not in MEMORY_SCOPES:
        raise AgentDefinitionError(f"{path.name}: memory_scope={meta['memory_scope']!r} 非法")
    if meta["output_contract"] not in OUTPUT_CONTRACTS:
        raise AgentDefinitionError(f"{path.name}: output_contract={meta['output_contract']!r} 非法")

    if not body:
        raise AgentDefinitionError(f"{path.name}: 正文为空")
    if not body.lstrip().startswith("你是"):
        raise AgentDefinitionError(f"{path.name}: 正文首句必须以「你是…」开头")

    lacking = [s for s in REQUIRED_SECTIONS if s not in body]
    if lacking:
        raise AgentDefinitionError(f"{path.name}: 正文缺章节 {lacking}")

    allow_tools = meta.get("allow_tools") or []
    aliases = meta.get("aliases") or []
    target_kinds = meta.get("target_kinds") or []
    stages = meta.get("stages") or []
    for key, value in (
        ("allow_tools", allow_tools),
        ("aliases", aliases),
        ("target_kinds", target_kinds),
        ("stages", stages),
    ):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise AgentDefinitionError(f"{path.name}: {key} 必须是字符串列表")
    unknown_targets = set(target_kinds) - TARGET_KINDS
    if unknown_targets:
        raise AgentDefinitionError(
            f"{path.name}: target_kinds 包含非法值 {sorted(unknown_targets)}"
        )
    if not aliases:
        raise AgentDefinitionError(f"{path.name}: aliases 至少要有一个可显示名称")

    max_output_tokens = meta.get("max_output_tokens")
    if max_output_tokens is not None and (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or max_output_tokens <= 0
    ):
        raise AgentDefinitionError(f"{path.name}: max_output_tokens 必须是正整数")

    return AgentDefinition(
        agent_code=str(meta["agent_code"]),
        capability=str(meta["capability"]),
        role=str(meta["role"]),
        role_type=str(meta["role_type"]),
        focusable=bool(meta["focusable"]),
        aliases=tuple(str(item).strip() for item in aliases if str(item).strip()),
        target_kinds=tuple(str(item) for item in target_kinds),
        stages=tuple(str(item) for item in stages),
        max_turns=int(meta["max_turns"]),
        conversational=bool(meta["conversational"]),
        memory_scope=str(meta["memory_scope"]),
        context_budget=int(meta["context_budget"]),
        output_contract=str(meta["output_contract"]),
        max_output_tokens=max_output_tokens,
        allow_tools=[str(t) for t in allow_tools],
        system_prompt=body,
        source_file=path.name,
        source_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def parse_agent_dir(directory: Path) -> list[AgentDefinition]:
    """解析目录下全部 *.md，按 agent_code 排序返回。"""
    files = sorted(p for p in directory.glob("*.md") if not p.name.startswith("_"))
    return [parse_agent_file(p) for p in files]


@lru_cache
def load_registry() -> dict[str, AgentDefinition]:
    """加载全部工程级 Agent 定义，进程内缓存。启动时调一次即作全量校验。"""
    definitions = parse_agent_dir(get_settings().agent_prompts_dir)
    return {d.agent_code: d for d in definitions}


def get_agent(agent_code: str) -> AgentDefinition:
    """取单个 Agent 定义，不存在即报错。Agent 清单固定，不做运行时增删。"""
    registry = load_registry()
    try:
        return registry[agent_code]
    except KeyError:
        raise AgentDefinitionError(
            f"未定义的 agent_code={agent_code!r}，已知：{sorted(registry)}"
        ) from None


def resolve_agent_alias(value: str) -> AgentDefinition | None:
    """按标准 code 或展示别名解析 Agent；比较时忽略首尾空白与英文大小写。"""
    wanted = value.strip().lstrip("@").casefold()
    if not wanted:
        return None
    for agent in load_registry().values():
        names = (agent.agent_code, agent.role, *agent.aliases)
        if any(wanted == name.strip().casefold() for name in names):
            return agent
    return None


def agents_for(target_kind: str, stage: str = "") -> tuple[AgentDefinition, ...]:
    """返回目标与阶段允许的 Agent 目录，供后端白名单和前端候选共用。"""
    return tuple(
        agent
        for agent in load_registry().values()
        if target_kind in agent.target_kinds and (not agent.stages or stage in agent.stages)
    )
