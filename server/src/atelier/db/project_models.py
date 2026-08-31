"""项目库（`{项目目录}/.atelier/project.db`）表定义。

一个项目 = 一个目录 + 目录里自带的这个库。项目可以放在磁盘任意位置（不必在本仓库
`assets/` 下），整个目录连库一起拷走、换机器挂上就还是那个项目：素材、状态、会话、
记忆、任务日志全在里面，不依赖本机那份全局 runtime.db。

因此本库**只存项目自己的东西**，不含任何机器级数据：provider 凭证、额度用量、路由
日志留在全局 runtime.db。跨库引用只存 code 字符串或裸 id，不建外键、不 join——
`Conversation.bound_provider_model_id` 指向的是另一个库的行，取不到就当需要重绑。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class ProjectBase(DeclarativeBase):
    """项目库独立 Base，与配置库、全局日志库的 metadata 完全隔离。"""

    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


# --------------------------------------------------------------------------- #
# 项目自身
# --------------------------------------------------------------------------- #


class ProjectMeta(ProjectBase):
    """本库属于哪个项目，以及项目的运行态。固定单行（id=1）。

    配置（名称、风格、defaults 等）的真相是目录里的 `project.json`，不在这里存副本；
    这里只放两件 json 放不下的东西：`project_code` 用来认亲（目录被整份复制成另一个
    项目时能发现库对不上），`state` 是立项工作流的推进状态。
    """

    __tablename__ = "project_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    project_code: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="P0_project_shaping")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Character(ProjectBase):
    """人物素材，状态与 meta.json 双写，断电可从 meta.json 恢复。"""

    __tablename__ = "characters"
    __table_args__ = (UniqueConstraint("name", name="uq_character_name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    dir_name: Mapped[str] = mapped_column(String(255))
    """相对项目目录的路径，如 `characters/赤瞳双尾兽`——项目目录整体搬走后仍然有效。"""
    state: Mapped[str] = mapped_column(String(32), default="S0_spec_drafting")
    spec_path: Mapped[str | None] = mapped_column(String(512), default=None)
    render_path: Mapped[str | None] = mapped_column(String(512), default=None)
    """定稿渲染图的相对路径。`generations` 里有全部候选，这里只记人采用的那一张——后续
    每一步都拿它当参考图，每次都去台账里筛一遍不如把结论存在角色行上。"""
    hard_constraints: Mapped[dict[str, Any]] = mapped_column(default=dict)
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    gate_spec_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    gate_render_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Generation(ProjectBase):
    """一条产物记录：中间产物与定稿都登记，便于回溯与归档。"""

    __tablename__ = "generations"
    __table_args__ = (Index("ix_generations_target", "target_kind", "target_ref"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_kind: Mapped[str] = mapped_column(String(32))
    target_ref: Mapped[str] = mapped_column(String(128))
    stage: Mapped[str] = mapped_column(String(32))
    variant: Mapped[str | None] = mapped_column(String(64), default=None)
    file_path: Mapped[str] = mapped_column(String(512))
    """相对项目目录，理由同 Character.dir_name。"""
    file_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32), default="generated")
    task_id: Mapped[str | None] = mapped_column(String(64), default=None)
    asset_spec: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# --------------------------------------------------------------------------- #
# 任务与事件
# --------------------------------------------------------------------------- #


class Task(ProjectBase):
    """一次工作流步骤的执行单元。"""

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_target", "target_kind", "target_ref"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
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


class TaskStep(ProjectBase):
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


class TaskEvent(ProjectBase):
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


class Conversation(ProjectBase):
    """一次与会话型 Agent 的对焦过程。

    轮转粒度是会话而不是每次调用：多轮对话里换 provider 等于让对方从零算一遍前缀、
    自己这边的缓存全部作废。所以会话首轮选定一个 provider_model 后就绑定在此，之后每轮
    复用，只有熔断、额度耗尽或该模型被删才换绑。单次调用型 Agent（conversational=false）
    没有前缀可复用，才按调用轮转。

    `bound_provider_model_id` 指向全局 runtime.db 的 `provider_models.id`，跨库故不设
    外键：模型被删后这里会剩一个悬空 id，选路时找不到候选就按「已绑模型不可用」换绑，
    与外键 SET NULL 的效果一致，且项目目录换机器后也不会因为 id 对不上而报错。
    """

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_target", "target_kind", "target_ref"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_kind: Mapped[str] = mapped_column(String(32))
    target_ref: Mapped[str | None] = mapped_column(String(128), default=None)
    agent_code: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    """新会话只会是 `active`：沉淀与丢弃都只处理草稿，不冻结会话。

    列留着是为了读得懂老库——那时沉淀过的会话会被置成 `committed`/`discarded`。
    """

    # 会话级粘性绑定
    bound_provider_model_id: Mapped[int | None] = mapped_column(Integer, default=None)
    bound_provider_label: Mapped[str] = mapped_column(String(255), default="")
    """绑的是谁的快照（`provider_code/model_id`），换机器后 id 失效时还能看懂日志。"""
    bound_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    rebind_count: Mapped[int] = mapped_column(Integer, default=0)
    rebind_reason: Mapped[str | None] = mapped_column(String(255), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Message(ProjectBase):
    """会话消息原文，只折叠不删除。

    assistant 那条在这一轮开跑时就先落一条空的（`status="thinking"`），模型回完再把内容
    填回同一行。「正在想」是一个跟着会话走的事实，只活在内存里的话，用户切走页面再回来就看不
    见了，进程重启更是直接丢。
    """

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
    status: Mapped[str] = mapped_column(String(16), default="done")
    """thinking=正在等回答、done=回答已落库、failed=这一轮炸了、cancelled=用户中断。

    只有 done 进上下文。
    """
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ConversationMemory(ProjectBase):
    """滚动摘要 + 已拍板结论 + 待确认问题。"""

    __tablename__ = "conversation_memory"

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    open_questions: Mapped[list[str]] = mapped_column(default=list)
    decisions: Mapped[list[str]] = mapped_column(default=list)
    folded_turns: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ArtifactDraft(ProjectBase):
    """未确认的产物草稿：只入库，不落盘。确认沉淀是唯一落盘入口。"""

    __tablename__ = "artifact_drafts"
    __table_args__ = (Index("ix_drafts_conv", "conversation_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    target_path: Mapped[str] = mapped_column(String(512))
    """相对项目目录的落盘位置，确认后才写到那里去。"""
    content: Mapped[str] = mapped_column(Text)
    based_on_hash: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ProjectMemory(ProjectBase):
    """项目长期记忆，可在设置页增删改。

    `character_ref` 把记忆分成两档：空串是项目级（注入所有 Agent），填了角色 id 就只注入
    那个角色的会话。不分的话，在「赤瞳」设定里说的「尾巴要 2 条」会跟着进下一个角色的提示词，
    用户得反复推翻一条他从没对这个角色说过的要求。

    用空串而不是 NULL 表示项目级：SQLite 的唯一索引里 NULL 彼此不相等，拿 NULL 当默认值等于
    把项目级记忆的去重关掉。
    """

    __tablename__ = "project_memory"
    __table_args__ = (UniqueConstraint("content_hash", "character_ref", name="uq_project_memory"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    character_ref: Mapped[str] = mapped_column(String(64), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source_conversation_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ProjectAgentPrompt(ProjectBase):
    """项目级 Agent 附加指令。

    工程级提示词在 atelier/prompts/agents/*.md，只读不可改；本表存用户为本项目给某
    Agent 补充的指令，组装上下文时追加在工程提示词之后，不覆盖、不改写。
    """

    __tablename__ = "project_agent_prompts"
    __table_args__ = (UniqueConstraint("agent_code", name="uq_project_agent_prompt"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_code: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ProjectPromptSnippet(ProjectBase):
    """项目自定义提示词片段：负向词、风格层等，与工程预设合并后使用。"""

    __tablename__ = "project_prompt_snippets"
    __table_args__ = (
        UniqueConstraint("code", name="uq_project_prompt_snippet"),
        Index("ix_project_prompt_snippet_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    slot: Mapped[str | None] = mapped_column(String(32), default=None)
    content: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_no: Mapped[int] = mapped_column(Integer, default=0)
    remark: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
