"""API 数据契约。

一条铁律：**响应体里绝不出现明文 api_key**，只给掩码与「配没配」两个事实。要把整套配置
交给别人时走导出接口并显式带上 `include_keys`，那是用户主动做出的选择。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from atelier.providers import period as period_mod
from atelier.providers.base import LIMIT_KINDS

LimitKind = Literal["tokens", "calls", "credits"]

DRIVERS = (
    "openai_compat",
    "dashscope_async",
    "dashscope_mm",
    "ark_image",
    "ark_video",
    "meshy",
)


class Schema(BaseModel):
    """protected_namespaces 清空：本项目的 `model_id` / `models` 是业务字段，不是 pydantic 内部。"""

    model_config = ConfigDict(protected_namespaces=(), from_attributes=True)


# --------------------------------------------------------------------------- #
# 额度
# --------------------------------------------------------------------------- #


class LimitIn(Schema):
    limit_kind: LimitKind
    max_value: int = Field(ge=0, description="0 或不配 = 不限量")
    group_name: str = "default"
    period_expr: str = "day"

    @field_validator("period_expr")
    @classmethod
    def _valid_period(cls, raw: str) -> str:
        return period_mod.normalize(raw)


class LimitOut(LimitIn):
    id: int
    window_text: str = ""


class BudgetOut(Schema):
    """额度看板的一行：当前窗口用了多少、还剩多少、这个数是谁给的。"""

    limit_kind: str
    limit: int
    used: int
    remaining: int | None
    available: int | None
    window_key: str
    window_text: str
    period_expr: str
    group_name: str
    source: str
    exhausted: bool
    unlimited: bool


class BreakerOut(Schema):
    open_until: str
    fail_count: int
    last_reason: str | None


# --------------------------------------------------------------------------- #
# provider 与模型
# --------------------------------------------------------------------------- #


class ModelIn(Schema):
    model_id: str = Field(min_length=1, max_length=128)
    capabilities: list[str] = Field(default_factory=lambda: ["text"])
    driver: str | None = Field(default=None, description="留空则继承 provider 的 driver")
    api_path: str | None = None
    enabled: bool = True
    sort_no: int = 0
    params: dict[str, Any] = Field(
        default_factory=dict,
        description='调用参数；键 credit_costs 存每种操作的积分单价，如 {"image_to_3d": 5}',
    )
    remark: str | None = None
    agents: list[str] = Field(default_factory=list, description="绑定到哪些 Agent")
    limits: list[LimitIn] = Field(default_factory=list)

    @field_validator("driver")
    @classmethod
    def _known_driver(cls, raw: str | None) -> str | None:
        if raw is not None and raw not in DRIVERS:
            raise ValueError(f"driver 只能是 {DRIVERS} 之一")
        return raw

    @field_validator("limits")
    @classmethod
    def _one_limit_per_kind(cls, rows: list[LimitIn]) -> list[LimitIn]:
        kinds = [row.limit_kind for row in rows]
        if len(kinds) != len(set(kinds)):
            raise ValueError("同一个模型每种计量口径只能配一条额度")
        return rows


class ModelOut(Schema):
    id: int
    provider_code: str
    model_id: str
    capabilities: list[str]
    driver: str | None
    effective_driver: str
    api_path: str | None
    endpoint: str
    enabled: bool
    sort_no: int
    params: dict[str, Any]
    remark: str | None
    agents: list[str]
    limits: list[LimitOut]


class ProviderIn(Schema):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = ""
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = ""
    enabled: bool = True
    priority: int = Field(default=100, description="升序，越小越先用")
    driver: str = "openai_compat"
    auth_style: Literal["bearer", "x-api-key"] = "bearer"
    verify_ssl: bool = True
    remark: str | None = None
    models: list[ModelIn] = Field(default_factory=list)

    @field_validator("driver")
    @classmethod
    def _known_driver(cls, raw: str) -> str:
        if raw not in DRIVERS:
            raise ValueError(f"driver 只能是 {DRIVERS} 之一")
        return raw

    @field_validator("models")
    @classmethod
    def _unique_models(cls, rows: list[ModelIn]) -> list[ModelIn]:
        ids = [row.model_id for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError("同一个 provider 下 model_id 不能重复")
        return rows


class ProviderPatch(Schema):
    """只改传进来的字段。api_key 传空串表示清空、不传表示不动。"""

    name: str | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=255)
    api_key: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    driver: str | None = None
    auth_style: Literal["bearer", "x-api-key"] | None = None
    verify_ssl: bool | None = None
    remark: str | None = None

    @field_validator("driver")
    @classmethod
    def _known_driver(cls, raw: str | None) -> str | None:
        if raw is not None and raw not in DRIVERS:
            raise ValueError(f"driver 只能是 {DRIVERS} 之一")
        return raw


class ProviderOut(Schema):
    code: str
    name: str
    base_url: str
    api_key_mask: str
    has_key: bool
    enabled: bool
    priority: int
    driver: str
    auth_style: str
    verify_ssl: bool
    remark: str | None
    models: list[ModelOut]


class ModelUsageOut(Schema):
    """额度看板的一个候选：它在各计量口径下的用量，以及是否正被熔断、缺不缺 key。"""

    provider_model_id: int
    provider_code: str
    provider_name: str
    provider_enabled: bool
    model_id: str
    enabled: bool
    has_key: bool
    priority: int
    agents: list[str]
    budgets: list[BudgetOut]
    breaker: BreakerOut | None


class UsageBoardOut(Schema):
    items: list[ModelUsageOut]
    limit_kinds: list[str] = Field(default_factory=lambda: list(LIMIT_KINDS))


# --------------------------------------------------------------------------- #
# 导入导出
# --------------------------------------------------------------------------- #


class ImportRequest(Schema):
    """provider_agents.json 格式的整包配置。

    mode=merge 只新增与更新，库里已有、包里没提到的 provider 留着不动；
    mode=replace 先清空全部 provider 再灌——会连带删掉它们的额度、用量与 Agent 绑定。
    """

    providers: dict[str, dict[str, Any]]
    mode: Literal["merge", "replace"] = "merge"


class ImportResult(Schema):
    created: list[str]
    updated: list[str]
    removed: list[str]
    models: int
    bindings: int
    limits: int
    warnings: list[str] = Field(default_factory=list)


class RouteLogOut(Schema):
    id: int
    ts: str
    agent_code: str
    provider_code: str | None
    model_id: str | None
    outcome: str
    reason: str | None
    attempt_no: int
    latency_ms: int | None
    used_delta: int | None
    limit_kind: str | None
    task_id: str | None
    conversation_id: str | None
    project_code: str | None
