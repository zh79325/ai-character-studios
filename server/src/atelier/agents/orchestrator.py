"""一场会话里的多 Agent 编排：阶段白名单、显式收件人与交接协议。

业务 Agent 路由与 Provider 路由严格分层：本模块只决定由哪个 Agent 工作，不接触模型候选。
角色阶段只由 ``Character`` 状态与人工门禁推导，模型输出不能修改状态机。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from atelier.agents.definitions import (
    AgentDefinition,
    agents_for,
    get_agent,
    load_registry,
)
from atelier.assets import characters as character_assets
from atelier.db.project_models import Character, Conversation
from atelier.errors import Conflict

DIRECTOR = "studio_director"
MAX_HANDOFFS = 2
ROUTE_ACTIONS = ("delegate", "status", "clarify")
HANDOFF_STATUSES = ("continue", "complete", "blocked", "handoff")

ROUTE_PROTOCOL = """[路由开始]
action: delegate | status | clarify
agent: <标准 Agent code；非 delegate 留空>
reason: <一句话理由>
[路由结束]"""

HANDOFF_PROTOCOL = """[交接开始]
status: continue | complete | blocked | handoff
agent: <handoff 时填写标准 Agent code，其他状态留空>
reason: <一句话说明>
[交接结束]"""


@dataclass(frozen=True, slots=True)
class RouteDecision:
    action: str
    agent_code: str = ""
    reason: str = ""
    source: str = "director"


@dataclass(frozen=True, slots=True)
class HandoffResult:
    status: str
    agent_code: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class HandoffEvent:
    turn_no: int
    from_agent_code: str
    to_agent_code: str
    source: str
    reason: str
    status: str = "delegated"


@dataclass(frozen=True, slots=True)
class ResolvedRecipient:
    agent_code: str
    source: str
    text: str


@dataclass(frozen=True)
class Stage:
    """一个阶段：谁主持、能派谁、推进它要哪一道人工确认。"""

    code: str
    label: str
    director: str
    """阶段总管。所有对象都使用 ``studio_director``，运行状态仍按 Conversation 隔离。"""
    crew: tuple[str, ...]
    """允许被指派的子 Agent。执行器要拿它当白名单，模型报了别的就拒掉。"""
    gate_field: str
    """推进这一阶段需要人工确认的列名，空串=这一阶段没有人工门禁。"""


STAGES: tuple[Stage, ...] = (
    Stage(
        code="spec",
        label="设定对焦",
        director=DIRECTOR,
        crew=("spec_writer", "spec_reviewer"),
        gate_field="gate_spec_confirmed_at",
    ),
    Stage(
        code="render",
        label="渲染图对焦",
        director=DIRECTOR,
        crew=("prompt_smith", "image_t2i", "image_i2i", "vision_reviewer"),
        gate_field="gate_render_confirmed_at",
    ),
    Stage(
        code="views",
        label="四视图对焦",
        director=DIRECTOR,
        crew=("prompt_smith", "image_i2i", "vision_reviewer"),
        gate_field="",
    ),
)
"""角色流程在会话里的三段，数据照 `seeds/workflow_defs.json` 的 `character_v1` 抄。

建模及之后（S6-S9）不进会话，仍走各自的任务接口。
"""

_BY_CODE = {one.code: one for one in STAGES}


def stage(code: str) -> Stage:
    """按 code 取阶段。code 不认识直接 KeyError：调用方只会传本文件里的常量。"""
    return _BY_CODE[code]


def stage_of(character: Character) -> str:
    """这个角色当下在哪一阶段。

    看的是两道人工门禁的既成事实，而不是 `state` 的字面值：门禁确认过就算过了那一段，之后
    无论重生几张图都还在同一段里。`state` 只用来兜住「四视图都确认完了」这种已经走出会话
    范围的情况。
    """
    if character.gate_render_confirmed_at is not None or character_assets.at_least(
        character, character_assets.VIEWS_CONFIRMED
    ):
        return "views"
    if character.gate_spec_confirmed_at is not None:
        return "render"
    return "spec"


def in_render_review(character: Character) -> bool:
    """这个角色现在处在效果图评审阶段：设定已确认、效果图还没定稿（S1/S2）。

    这一段里用户在会话里说的话就是「对这张图的修改要求」，`send()` 据此把它转成一轮重画而
    不是文本对话——设定已经拍板，此时再跟 `spec_writer` 逐字讨论改的是错的东西。
    """
    return (
        character_assets.at_least(character, character_assets.SPEC_CONFIRMED)
        and character.gate_render_confirmed_at is None
    )


def actor_for(conversation: Conversation) -> str:
    """当前实际收件 Agent；无专业焦点时回到该对象自己的总管。"""
    return conversation.focus_agent_code or conversation.agent_code or DIRECTOR


def available_agents(target_kind: str, stage_code: str) -> tuple[AgentDefinition, ...]:
    """当前目标和阶段可见、可指派的 Agent 目录。"""
    return agents_for(target_kind, stage_code)


def allowed_agent_codes(target_kind: str, stage_code: str) -> frozenset[str]:
    return frozenset(agent.agent_code for agent in available_agents(target_kind, stage_code))


def validate_recipient(agent_code: str, target_kind: str, stage_code: str) -> AgentDefinition:
    """校验显式/模型指派的目标；总管不受阶段限制，其余必须在阶段白名单。"""
    agent = get_agent(agent_code)
    if agent.agent_code == DIRECTOR:
        return agent
    if agent.agent_code not in allowed_agent_codes(target_kind, stage_code):
        raise Conflict(f"Agent {agent_code} 不能处理 {target_kind} 的 {stage_code} 阶段")
    return agent


def explicit_recipient(text: str) -> tuple[AgentDefinition | None, str]:
    """解析自然语言里的 ``@别名`` 兼容路径，并从交给 Agent 的正文中剥掉该 mention。"""
    for agent in load_registry().values():
        names = sorted({agent.agent_code, agent.role, *agent.aliases}, key=len, reverse=True)
        for name in names:
            marker = f"@{name}"
            index = text.casefold().find(marker.casefold())
            if index < 0:
                continue
            before = text[:index]
            after = text[index + len(marker) :]
            if after and not (after[0].isspace() or after[0] in "，。！？,:："):
                continue
            return agent, f"{before}{after}".strip()
    return None, text.strip()


def resolve_recipient(
    conversation: Conversation,
    *,
    target_kind: str,
    stage_code: str,
    text: str,
    recipient_agent_code: str | None = None,
) -> ResolvedRecipient:
    """按 ``@指定 > 当前焦点 > 总管`` 的固定优先级决定本轮收件人。"""
    mentioned, cleaned = explicit_recipient(text)
    explicit = get_agent(recipient_agent_code) if recipient_agent_code else mentioned
    if explicit is not None:
        validate_recipient(explicit.agent_code, target_kind, stage_code)
        return ResolvedRecipient(explicit.agent_code, "@", cleaned)
    if conversation.focus_agent_code:
        try:
            validate_recipient(conversation.focus_agent_code, target_kind, stage_code)
        except Conflict:
            conversation.focus_agent_code = None
            conversation.focus_started_at = None
            conversation.focus_reason = None
        else:
            return ResolvedRecipient(conversation.focus_agent_code, "focus", text.strip())
    return ResolvedRecipient(DIRECTOR, "director", text.strip())


def set_focus(conversation: Conversation, agent_code: str | None, reason: str = "") -> None:
    """设置或退出粘性焦点；只有声明可对焦的专业 Agent 可以进入。"""
    if not agent_code or agent_code == DIRECTOR:
        conversation.focus_agent_code = None
        conversation.focus_started_at = None
        conversation.focus_reason = None
        return
    agent = get_agent(agent_code)
    if not agent.focusable:
        conversation.focus_agent_code = None
        conversation.focus_started_at = None
        conversation.focus_reason = None
        return
    conversation.focus_agent_code = agent.agent_code
    conversation.focus_started_at = datetime.now(UTC)
    conversation.focus_reason = reason[:255] or None
