"""日志库（db/runtime.db，本地不进 Git）表定义。

含 provider 凭证、额度用量、项目与素材状态、任务与事件、会话与记忆。
禁止与配置库 join，跨库引用只存 code 字符串。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
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
    """日志库独立 Base，与配置库 metadata 完全隔离。"""

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
# 项目与素材
# --------------------------------------------------------------------------- #


class Project(RuntimeBase):
    """project.json 的可查询副本，磁盘为真相，启动时幂等同步。"""

    __tablename__ = "projects"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    dir_path: Mapped[str] = mapped_column(String(512))
    style: Mapped[dict[str, Any]] = mapped_column(default=dict)
    defaults: Mapped[dict[str, Any]] = mapped_column(default=dict)
    pose_template: Mapped[str | None] = mapped_column(String(512), default=None)
    art_bible: Mapped[str] = mapped_column(String(255), default="art-bible.md")
    review_mode: Mapped[str] = mapped_column(String(16), default="lean")
    state: Mapped[str] = mapped_column(String(32), default="P0_project_shaping")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Character(RuntimeBase):
    """人物素材，状态与 meta.json 双写，断电可从 meta.json 恢复。"""

    __tablename__ = "characters"
    __table_args__ = (
        UniqueConstraint("project_code", "name", name="uq_character_name"),
        Index("ix_characters_project", "project_code"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    dir_path: Mapped[str] = mapped_column(String(512))
    state: Mapped[str] = mapped_column(String(32), default="S0_spec_drafting")
    spec_path: Mapped[str | None] = mapped_column(String(512), default=None)
    hard_constraints: Mapped[dict[str, Any]] = mapped_column(default=dict)
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    gate_spec_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    gate_render_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Generation(RuntimeBase):
    """一条产物记录：中间产物与定稿都登记，便于回溯与归档。"""

    __tablename__ = "generations"
    __table_args__ = (Index("ix_generations_target", "project_code", "target_kind", "target_ref"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_code: Mapped[str] = mapped_column(String(64))
    target_kind: Mapped[str] = mapped_column(String(32))
    target_ref: Mapped[str] = mapped_column(String(128))
    stage: Mapped[str] = mapped_column(String(32))
    variant: Mapped[str | None] = mapped_column(String(64), default=None)
    file_path: Mapped[str] = mapped_column(String(512))
    file_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32), default="generated")
    task_id: Mapped[str | None] = mapped_column(String(64), default=None)
    asset_spec: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# --------------------------------------------------------------------------- #
# 任务与事件
# --------------------------------------------------------------------------- #


class Task(RuntimeBase):
    """一次工作流步骤的执行单元。"""

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_project", "project_code"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_code: Mapped[str] = mapped_column(String(64))
    target_kind: Mapped[str] = mapped_column(String(32))
    target_ref: Mapped[str] = mapped_column(String(128))
    stage: Mapped[str] = mapped_column(String(32))
    agent_code: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    params_snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class TaskStep(RuntimeBase):
    """任务内的子步骤，如四视图的每一张。"""

    __tablename__ = "task_steps"
    __table_args__ = (Index("ix_task_steps_task", "task_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64))
    step_no: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    external_task_id: Mapped[str | None] = mapped_column(String(128), default=None)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class TaskEvent(RuntimeBase):
    """运行日志与门禁决策，SSE 推给前端的数据源，写入前已脱敏。"""

    __tablename__ = "task_events"
    __table_args__ = (Index("ix_task_events_task_seq", "task_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)
    level: Mapped[str] = mapped_column(String(16), default="info")
    event: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)


# --------------------------------------------------------------------------- #
# 会话与记忆
# --------------------------------------------------------------------------- #


class Conversation(RuntimeBase):
    """一次与会话型 Agent 的对焦过程。

    provider 轮转的粒度是会话，不是单次调用：多轮对话每轮都要重发前缀上下文，
    换 provider 等于让对方从零算一遍前缀、自己这边的缓存全部作废。所以会话首轮选定
    一个 provider_model 后就绑定在此，之后每轮复用，只有熔断、额度耗尽或该模型被删
    才换绑。单次调用型 Agent（conversational=false）没有前缀可复用，才按调用轮转。
    """

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_target", "project_code", "target_kind", "target_ref"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_code: Mapped[str] = mapped_column(String(64))
    target_kind: Mapped[str] = mapped_column(String(32))
    target_ref: Mapped[str | None] = mapped_column(String(128), default=None)
    agent_code: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")

    # 会话级粘性绑定
    bound_provider_model_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_models.id", ondelete="SET NULL"), default=None
    )
    bound_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    rebind_count: Mapped[int] = mapped_column(Integer, default=0)
    rebind_reason: Mapped[str | None] = mapped_column(String(255), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    bound_provider_model: Mapped[ProviderModel | None] = relationship(lazy="selectin")


class Message(RuntimeBase):
    """会话消息原文，只折叠不删除。"""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "turn_no", name="uq_message_turn"),
        Index("ix_messages_conv", "conversation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    turn_no: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    folded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ConversationMemory(RuntimeBase):
    """滚动摘要 + 已拍板结论 + 待确认问题。"""

    __tablename__ = "conversation_memory"

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    open_questions: Mapped[list[str]] = mapped_column(default=list)
    decisions: Mapped[list[str]] = mapped_column(default=list)
    folded_turns: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ArtifactDraft(RuntimeBase):
    """未确认的产物草稿：只入库，不落盘。确认沉淀是唯一落盘入口。"""

    __tablename__ = "artifact_drafts"
    __table_args__ = (Index("ix_drafts_conv", "conversation_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    target_path: Mapped[str] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text)
    based_on_hash: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ProjectMemory(RuntimeBase):
    """项目长期记忆，注入所有 Agent 的 prompt，可在设置页增删改。"""

    __tablename__ = "project_memory"
    __table_args__ = (
        UniqueConstraint("project_code", "content_hash", name="uq_project_memory"),
        Index("ix_project_memory_project", "project_code"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_code: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source_conversation_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ProjectAgentPrompt(RuntimeBase):
    """项目级 Agent 附加指令。

    工程级提示词在 atelier/prompts/agents/*.md，只读不可改；本表存用户为某项目
    给某 Agent 补充的指令，组装上下文时追加在工程提示词之后，不覆盖、不改写。
    """

    __tablename__ = "project_agent_prompts"
    __table_args__ = (
        UniqueConstraint("project_code", "agent_code", name="uq_project_agent_prompt"),
        Index("ix_project_agent_prompt_project", "project_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_code: Mapped[str] = mapped_column(String(64))
    agent_code: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ProjectPromptSnippet(RuntimeBase):
    """项目自定义提示词片段：负向词、风格层等，与工程预设合并后使用。"""

    __tablename__ = "project_prompt_snippets"
    __table_args__ = (
        UniqueConstraint("project_code", "code", name="uq_project_prompt_snippet"),
        Index("ix_project_prompt_snippet_project", "project_code", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_code: Mapped[str] = mapped_column(String(64))
    code: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    slot: Mapped[str | None] = mapped_column(String(32), default=None)
    content: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_no: Mapped[int] = mapped_column(Integer, default=0)
    remark: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class AppSetting(RuntimeBase):
    """本机偏好：Unity 可执行路径、并发数、当前项目等。"""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
