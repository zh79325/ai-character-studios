"""全局日志库（db/runtime.db，本地不进 Git）表定义。

这里只放**机器级**数据：provider 凭证、Agent 绑定、额度用量、熔断、路由日志，加上
「本机打开过哪些项目」的注册表与本机偏好。它们跟着这台机器走，不跟着项目走。

项目自己的东西（素材、状态、任务、会话、记忆）在各项目目录下的 `.atelier/project.db`，
见 `project_models.py`——项目目录连库整体搬走仍是同一个项目。

禁止与配置库、项目库 join，跨库引用只存 code 字符串或裸 id。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(UTC)


class RuntimeBase(DeclarativeBase):
    """全局日志库独立 Base，与配置库、项目库的 metadata 完全隔离。"""

    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


# --------------------------------------------------------------------------- #
# provider 与路由
# --------------------------------------------------------------------------- #


class Provider(RuntimeBase):
    """一条 provider = 一个供应商账号/端点。

    主信息四维：名称（code / name）、base_url、api_key、支持的模型列表（models）。
    含明文 api_key，绝不进 Git。
    """

    __tablename__ = "providers"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    base_url: Mapped[str] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    driver: Mapped[str] = mapped_column(String(32), default="openai_compat")
    auth_style: Mapped[str] = mapped_column(String(16), default="bearer")
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    models: Mapped[list[ProviderModel]] = relationship(
        back_populates="provider",
        cascade="all, delete-orphan",
        order_by="ProviderModel.sort_no",
        lazy="selectin",
    )


class ProviderModel(RuntimeBase):
    """provider 支持的模型列表，一条 = 该 provider 下一个可调用的 model。

    Agent 绑定、额度、用量、熔断均挂在本记录上，删 provider 时级联清理。
    """

    __tablename__ = "provider_models"
    __table_args__ = (UniqueConstraint("provider_code", "model_id", name="uq_provider_model"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_code: Mapped[str] = mapped_column(
        ForeignKey("providers.code", ondelete="CASCADE"), index=True
    )
    model_id: Mapped[str] = mapped_column(String(128))
    capabilities: Mapped[list[str]] = mapped_column(default=list)
    driver: Mapped[str | None] = mapped_column(String(32), default=None)
    api_path: Mapped[str | None] = mapped_column(String(255), default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_no: Mapped[int] = mapped_column(Integer, default=0)
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    """调用参数与积分单价。约定键 `credit_costs`：{操作名: 消耗积分}，
    如 Meshy 的 {"image_to_3d": 5, "animate": 10}——消耗调用前已知，可预扣。"""
    remark: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    provider: Mapped[Provider] = relationship(back_populates="models")
    limits: Mapped[list[ModelLimit]] = relationship(
        back_populates="provider_model", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def effective_driver(self) -> str:
        """模型未指定时继承 provider 的 driver。"""
        return self.driver or self.provider.driver

    def endpoint(self) -> str:
        """拼出该模型的调用地址：provider.base_url + api_path。

        同一账号下文本与生图端点往往不同（如百炼 Token Plan 文本走
        /compatible-mode/v1、生图走 /api/v1/services/aigc/...），故路径挂到模型上。
        """
        base = self.provider.base_url.rstrip("/")
        if not self.api_path:
            return base
        return f"{base}/{self.api_path.lstrip('/')}"

    def credit_cost(self, operation: str) -> int:
        """该操作要预扣多少积分，未配置返回 0（不扣、不拦）。"""
        costs = self.params.get("credit_costs") or {}
        if not isinstance(costs, dict):
            return 0
        try:
            return max(int(costs.get(operation, 0)), 0)
        except (TypeError, ValueError):
            return 0


class ProviderAgentModel(RuntimeBase):
    """Agent → provider 模型 的绑定，同一 Agent 可挂多个候选。"""

    __tablename__ = "provider_agent_models"
    __table_args__ = (
        UniqueConstraint("agent_code", "provider_model_id", name="uq_pam"),
        Index("ix_pam_agent", "agent_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_code: Mapped[str] = mapped_column(String(64))
    provider_model_id: Mapped[int] = mapped_column(
        ForeignKey("provider_models.id", ondelete="CASCADE"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)

    provider_model: Mapped[ProviderModel] = relationship(lazy="selectin")


class ModelLimit(RuntimeBase):
    """额度上限与窗口口径，未配置视为无限额。

    max_value 与 period_expr 是**本地配置为准**的真相：远程用量服务返回的 limit 只是
    上一次记账时的快照，上限调大后它还是旧值，照抄会把新额度按回旧上限。
    """

    __tablename__ = "model_limits"
    __table_args__ = (UniqueConstraint("provider_model_id", "limit_kind", name="uq_model_limit"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_model_id: Mapped[int] = mapped_column(
        ForeignKey("provider_models.id", ondelete="CASCADE"), index=True
    )
    limit_kind: Mapped[str] = mapped_column(String(16))
    max_value: Mapped[int] = mapped_column(Integer)
    group_name: Mapped[str] = mapped_column(String(64), default="default")
    period_expr: Mapped[str] = mapped_column(String(32), default="day")

    provider_model: Mapped[ProviderModel] = relationship(back_populates="limits")


class UsageCounter(RuntimeBase):
    """按窗口分桶的用量镜像，跨窗口自动归零。

    真相在远程用量服务（多机共享同一份额度，不会各记一套），本表是它的本地镜像：
    远程返回即整条覆写，远程挂掉时本表接着拦。source 记下这一行的口径来源。
    """

    __tablename__ = "usage_counters"
    __table_args__ = (
        UniqueConstraint("provider_model_id", "limit_kind", "window_key", name="uq_usage_window"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_model_id: Mapped[int] = mapped_column(
        ForeignKey("provider_models.id", ondelete="CASCADE"), index=True
    )
    limit_kind: Mapped[str] = mapped_column(String(16))
    window_key: Mapped[str] = mapped_column(String(32))
    """窗口标签，与远程用量服务的 limitKey 同名同算法，换窗口即换行、旧行自然作废。"""
    used_value: Mapped[int] = mapped_column(Integer, default=0)
    remaining_value: Mapped[int | None] = mapped_column(Integer, default=None)
    """供应商或用量服务报告的剩余额度，拿不到就是 None（视为无限额）。"""
    exhausted_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    """本窗口判定用尽的时刻，非空即在窗口内跳过该候选。"""
    source: Mapped[str] = mapped_column(String(16), default="local")
    """remote / local / header，排查用量对不上时看这里。"""
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class CircuitBreaker(RuntimeBase):
    """候选级熔断：失败后短期跳过，到期自动恢复。"""

    __tablename__ = "circuit_breakers"
    __table_args__ = (UniqueConstraint("provider_model_id", name="uq_breaker"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_model_id: Mapped[int] = mapped_column(
        ForeignKey("provider_models.id", ondelete="CASCADE"), index=True
    )
    open_until: Mapped[datetime] = mapped_column(DateTime)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reason: Mapped[str | None] = mapped_column(Text, default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class RouteLog(RuntimeBase):
    """每次路由决策与失败原因，写入前已脱敏。

    outcome 区分粘性效果：sticky_hit（复用会话已绑模型，无缓存损耗）、
    bound（会话首次绑定）、rebound（被迫换绑，前缀缓存作废）、selected（无会话的
    单次调用轮转）、rejected / failed。rebound 的条数就是缓存损耗的直接度量。
    """

    __tablename__ = "route_logs"
    __table_args__ = (Index("ix_route_logs_task", "task_id"), Index("ix_route_logs_ts", "ts"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)
    agent_code: Mapped[str] = mapped_column(String(64))
    provider_code: Mapped[str | None] = mapped_column(String(64), default=None)
    model_id: Mapped[str | None] = mapped_column(String(128), default=None)
    group_name: Mapped[str | None] = mapped_column(String(64), default=None)
    outcome: Mapped[str] = mapped_column(String(16), default="selected")
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    used_delta: Mapped[int | None] = mapped_column(Integer, default=None)
    limit_kind: Mapped[str | None] = mapped_column(String(16), default=None)
    task_id: Mapped[str | None] = mapped_column(String(64), default=None)
    conversation_id: Mapped[str | None] = mapped_column(String(64), default=None)
    project_code: Mapped[str | None] = mapped_column(String(64), default=None)


# --------------------------------------------------------------------------- #
# 项目注册表与本机偏好
# --------------------------------------------------------------------------- #


class ProjectRegistry(RuntimeBase):
    """本机打开过哪些项目，以及它们在磁盘上的位置。

    项目的真相全在自己的目录里（`project.json` + `.atelier/project.db`），目录可以放在
    磁盘任意位置。本表只是一份「最近打开」的索引，丢了也不影响项目本身：重新指向目录
    导入一次就恢复。所以这里不存任何项目内容，只存路径与上次打开时间。
    """

    __tablename__ = "project_registry"
    __table_args__ = (UniqueConstraint("dir_path", name="uq_project_dir"),)

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    dir_path: Mapped[str] = mapped_column(String(1024))
    """项目目录的绝对路径。默认项目根下的项目也存绝对路径，口径只有一种。"""
    managed: Mapped[bool] = mapped_column(Boolean, default=True)
    """true = 位于默认项目根（仓库 assets/），扫描时自动登记；false = 从别处导入的。"""
    missing: Mapped[bool] = mapped_column(Boolean, default=False)
    """上次同步时目录不见了（外置盘没挂、被搬走）。只标记不删记录，等它回来。"""
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class AppSetting(RuntimeBase):
    """本机偏好：Unity 可执行路径、并发数、当前项目等。"""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
