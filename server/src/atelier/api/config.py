"""只读元数据：配置库的候选清单，加上代码资产里的 Agent 与提示词。

配置库由 seeds/ 幂等灌入，Agent 定义与提示词是代码资产，两者 UI 都不改，所以这里
只有 GET。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from atelier.agents import prompt_assets
from atelier.agents.definitions import AgentDefinition, load_registry
from atelier.api.deps import ConfigDb
from atelier.api.schemas import DRIVERS, Schema
from atelier.db.config_models import (
    AssetCategory,
    MeshyAction,
    ModelCatalog,
    WorkflowDef,
)
from atelier.providers import period as period_mod
from atelier.providers.base import LIMIT_KINDS

router = APIRouter(prefix="/api/config", tags=["config"])


class AgentOut(Schema):
    agent_code: str
    capability: str
    role: str
    role_type: str
    focusable: bool
    aliases: list[str]
    target_kinds: list[str]
    stages: list[str]
    max_turns: int
    conversational: bool
    memory_scope: str
    context_budget: int
    output_contract: str
    allow_tools: list[str]
    source_file: str
    system_prompt: str | None = None


class PromptTemplateOut(Schema):
    code: str
    category: str
    slot: str
    content: str
    sort_no: int
    remark: str | None


class NegativePresetOut(Schema):
    code: str
    scene: str
    content: str
    remark: str | None


class ModelCatalogOut(Schema):
    id: int
    vendor: str
    plan: str
    preset_code: str
    driver: str
    model_id: str
    capabilities: list[str]
    limit_kind: str
    default_period: str
    base_url: str | None
    api_path: str | None
    auth_style: str
    key_prefix: str | None
    remark: str | None


class AssetCategoryOut(Schema):
    code: str
    name: str
    dir_name: str
    sort_no: int
    enabled: bool


class WorkflowOut(Schema):
    code: str
    target_kind: str
    name: str
    states: list[str]
    remark: str | None


class MeshyActionOut(Schema):
    action_id: str
    name: str
    category: str
    sub_category: str | None
    description: str | None


class OptionsOut(Schema):
    """设置页下拉框的取值域，前端不再各自硬编码一份。"""

    drivers: list[str]
    limit_kinds: list[str]
    auth_styles: list[str]
    period_units: list[str]
    period_examples: dict[str, str]


@router.get("/options", response_model=OptionsOut)
def options() -> OptionsOut:
    return OptionsOut(
        drivers=list(DRIVERS),
        limit_kinds=list(LIMIT_KINDS),
        auth_styles=["bearer", "x-api-key"],
        period_units=[*period_mod.REMOTE_UNITS, *period_mod.LOCAL_UNITS],
        period_examples={
            "day": "每天 0 点重置",
            "day+11H": "每天 11 点重置（方舟免费额度就是这种）",
            "month": "每月 1 号重置",
            "hour": "每小时重置（本地统计，不发远程用量服务）",
            "week": "每周一重置（本地统计）",
            "total": "买断式总量，永不重置（Meshy 积分池）",
        },
    )


@router.get("/agents", response_model=list[AgentOut])
def list_agents(
    include_prompt: bool = Query(default=False, description="带上完整系统提示词，比较长"),
) -> list[AgentOut]:
    """Agent 清单来自 prompts/agents/*.md，不是数据库——它是不可改的工程资产。"""
    registry = load_registry()
    return [_agent_out(registry[code], include_prompt) for code in sorted(registry)]


def _agent_out(row: AgentDefinition, include_prompt: bool) -> AgentOut:
    return AgentOut(
        agent_code=row.agent_code,
        capability=row.capability,
        role=row.role,
        role_type=row.role_type,
        focusable=row.focusable,
        aliases=list(row.aliases),
        target_kinds=list(row.target_kinds),
        stages=list(row.stages),
        max_turns=row.max_turns,
        conversational=row.conversational,
        memory_scope=row.memory_scope,
        context_budget=row.context_budget,
        output_contract=row.output_contract,
        allow_tools=list(row.allow_tools),
        source_file=row.source_file,
        system_prompt=row.system_prompt if include_prompt else None,
    )


@router.get("/prompt-templates", response_model=list[PromptTemplateOut])
def list_prompt_templates(
    slot: str | None = Query(default=None),
) -> list[PromptTemplateOut]:
    rows = prompt_assets.load_prompt_templates()
    return [
        PromptTemplateOut.model_validate(row, from_attributes=True)
        for row in rows
        if slot is None or row.slot == slot
    ]


@router.get("/negative-presets", response_model=list[NegativePresetOut])
def list_negative_presets() -> list[NegativePresetOut]:
    rows = prompt_assets.load_negative_presets()
    return [NegativePresetOut.model_validate(row, from_attributes=True) for row in rows]


@router.get("/model-catalog", response_model=list[ModelCatalogOut])
def list_model_catalog(
    session: ConfigDb,
    vendor: str | None = Query(default=None),
    capability: str | None = Query(default=None, description="按能力筛，如 t2i"),
) -> list[ModelCatalogOut]:
    stmt = select(ModelCatalog).order_by(
        ModelCatalog.vendor, ModelCatalog.plan, ModelCatalog.model_id
    )
    if vendor:
        stmt = stmt.where(ModelCatalog.vendor == vendor)
    rows = session.scalars(stmt).all()
    # capabilities 是 JSON 数组，SQLite 侧没有可靠的包含查询，而这张表就几十行
    return [
        ModelCatalogOut.model_validate(row)
        for row in rows
        if capability is None or capability in row.capabilities
    ]


@router.get("/asset-categories", response_model=list[AssetCategoryOut])
def list_asset_categories(session: ConfigDb) -> list[AssetCategoryOut]:
    rows = session.scalars(select(AssetCategory).order_by(AssetCategory.sort_no)).all()
    return [AssetCategoryOut.model_validate(row) for row in rows]


@router.get("/workflows", response_model=list[WorkflowOut])
def list_workflows(session: ConfigDb) -> list[WorkflowOut]:
    rows = session.scalars(select(WorkflowDef).order_by(WorkflowDef.code)).all()
    return [WorkflowOut.model_validate(row) for row in rows]


@router.get("/meshy-actions", response_model=list[MeshyActionOut])
def list_meshy_actions(
    session: ConfigDb,
    category: str | None = Query(default=None),
    q: str | None = Query(default=None, description="按动作名模糊匹配"),
    limit: int = Query(default=200, ge=1, le=2000),
) -> list[MeshyActionOut]:
    stmt = select(MeshyAction).order_by(MeshyAction.category, MeshyAction.name)
    if category:
        stmt = stmt.where(MeshyAction.category == category)
    if q:
        stmt = stmt.where(MeshyAction.name.ilike(f"%{q}%"))
    rows = session.scalars(stmt.limit(limit)).all()
    return [MeshyActionOut.model_validate(row) for row in rows]
