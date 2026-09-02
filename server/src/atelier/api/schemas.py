"""API 数据契约。

一条铁律：**响应体里绝不出现明文 api_key**，只给掩码与「配没配」两个事实。要把整套配置
交给别人时走导出接口并显式带上 `include_keys`，那是用户主动做出的选择。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from atelier.providers import period as period_mod
from atelier.providers.base import LIMIT_KINDS

LimitKind = Literal["tokens", "calls", "credits", "images"]

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
# 新建账号的预设
# --------------------------------------------------------------------------- #


class PresetModelOut(Schema):
    """预设里的一个模型。额度数字不给默认值：套餐买了多少只有用户自己知道。"""

    model_id: str
    capabilities: list[str]
    driver: str
    api_path: str | None
    limit_kind: str
    default_period: str
    params: dict[str, Any] = Field(default_factory=dict)
    """调用参数的预置初值，含上下文窗口 `context_window`，建账号时原样带走。"""
    remark: str | None


class ProviderPresetOut(Schema):
    """一个套餐一份预设。`code` 是建议值，同一套餐开两个账号得各起一个。"""

    code: str
    vendor: str
    plan: str
    label: str
    base_url: str
    driver: str
    auth_style: str
    key_prefix: str | None
    models: list[PresetModelOut]


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


# --------------------------------------------------------------------------- #
# 项目
# --------------------------------------------------------------------------- #


class ProjectSummaryOut(Schema):
    """项目列表的一行。带绝对路径：项目可以在磁盘任意位置，用户靠它分辨同名项目。"""

    code: str
    name: str
    dir_path: str
    managed: bool
    missing: bool
    stage: Literal["drafting", "ready"] = "ready"
    """`drafting` = 还在立项对焦、名字与骨架都没定。"""


class ProjectListOut(Schema):
    projects: list[ProjectSummaryOut]
    default_root: str
    """默认项目根（仓库 `assets/`），前端新建时拿它做目录预填。"""


class ProjectStyleIn(Schema):
    model_config = ConfigDict(protected_namespaces=(), from_attributes=True, extra="allow")

    art_style: str = ""
    mood: str = ""
    palette: str = ""
    quality: str = ""


class ProjectDefaultsIn(Schema):
    model_config = ConfigDict(protected_namespaces=(), from_attributes=True, extra="allow")

    image_size: int = Field(default=2048, ge=256, le=8192)
    texture_resolution: str = "2k"
    enable_pbr: bool = True
    target_polycount: int = Field(default=30000, ge=1000)
    pose_mode: str = "t-pose"
    height_meters: float = Field(default=1.7, gt=0)


class ProjectConfigOut(Schema):
    """`project.json` 原样吐回。它是项目配置的唯一真相，库里不存副本。"""

    model_config = ConfigDict(protected_namespaces=(), from_attributes=True, extra="allow")

    code: str
    name: str
    style: ProjectStyleIn
    defaults: ProjectDefaultsIn
    pose_template: str | None = None
    art_bible: str = "art-bible.md"
    review_mode: Literal["full", "lean", "solo"] = "lean"
    conversation_audit: bool = True
    stage: Literal["drafting", "ready"] = "ready"
    state: str = "P0_project_shaping"


class ProjectConfigPatch(Schema):
    """项目配置表单的提交体。code 是跟着目录走的身份，不得改，因此不在这里。"""

    name: str | None = None
    style: ProjectStyleIn | None = None
    defaults: ProjectDefaultsIn | None = None
    pose_template: str | None = None
    review_mode: Literal["full", "lean", "solo"] | None = None
    conversation_audit: bool | None = None


class ProjectBootstrapIn(Schema):
    dir_path: str = Field(
        min_length=1, description="项目产出落地的目录（可以还不存在，里面本来有东西也行）"
    )
    overwrite: bool = False
    """真则先抹掉目录里旧项目的 `project.json`、`art-bible.md` 与 `.atelier/` 再建。

    素材文件不动。只在用户对着确认框点过头之后才该带上。"""


class ProjectDirStateOut(Schema):
    """候选目录的现状。新建前先问一次，占着就先让用户点头再覆盖。"""

    occupied: bool
    marks: list[str]
    """占着这块地的那几个文件名（`project.json` / `art-bible.md`）。"""
    is_project: bool


class ProjectFinalizeIn(Schema):
    """立项收口：名字与代号是对焦完之后用户选定的，不是建项目时填的。"""

    name: str = Field(min_length=1, max_length=100)
    code: str = Field(min_length=1, max_length=64)


class ProjectImportIn(Schema):
    dir_path: str = Field(min_length=1, description="已带 project.json 的项目目录")


class ArtBibleOut(Schema):
    path: str
    content: str
    forbidden: list[str] = Field(
        default_factory=list,
        description="「风格禁止项」一节抽出的条目，生图时拼进 negative_prompt",
    )


class ArtBibleIn(Schema):
    content: str


class MissingCharacterOut(Schema):
    id: str
    name: str
    dir_name: str


class ScanResultOut(Schema):
    added: list[str]
    missing: list[MissingCharacterOut]
    """库里有而磁盘上没的角色，前端提供逐条手动删除入口。"""
    total: int


class ConstraintOut(Schema):
    item: str
    value: str


class CharacterOut(Schema):
    id: str
    name: str
    dir_name: str
    state: str
    state_label: str
    """状态的中文说法。前端不该自己维护一份状态码到人话的映射，两处总会对不齐。"""
    spec_path: str | None = None
    render_path: str | None = None
    """定稿渲染图。前端据此在列表里直接显缩略图。"""
    view_paths: dict[str, str] = Field(default_factory=dict)
    """定稿的四视图 `{视角: 相对路径}`。没定稿就是空的，建模那一步吃的就是这四张。"""
    hard_constraints: list[ConstraintOut] = Field(default_factory=list)
    """最近一次评审翻译出来的硬约束。后续每张图对着它逐条判定。"""
    gate_spec_confirmed_at: str | None = None
    gate_render_confirmed_at: str | None = None
    updated_at: str


class CharacterCreateIn(Schema):
    name: str = Field(min_length=1, max_length=120)
    group: str = Field(
        default="",
        max_length=512,
        description="建在哪个分组下（相对 characters/ 的路径，可多级），空串是根",
    )
    overwrite: bool = Field(
        default=False, description="目标目录已存在时：为真就删旧重建，为假就报冲突"
    )


class GroupCreateIn(Schema):
    path: str = Field(
        min_length=1, max_length=512, description="要建的分组路径，相对 characters/，可多级"
    )


class SpecReviewIn(Schema):
    conversation_id: str | None = Field(
        default=None, description="驳回后自动重生要用的设定会话；不给就只审一次"
    )


class SpecReviewOut(Schema):
    character_id: str
    decision: str
    approved: bool
    attempt: int
    regenerated: int
    manual: bool
    """自动重生用尽仍未通过：界面上要把问题摆给用户，让他改 art bible 或自己动手。"""
    sections: dict[str, list[str]]
    constraints: list[ConstraintOut]
    text: str
    """裁决全文。卡片上原样展示——摘一句话用户判断不了该不该放行。"""


class GateIn(Schema):
    note: str = Field(default="", max_length=2000)


class AssetSpecOut(Schema):
    """一张素材规格卡片。`card` 是原文，前端要能把模型写的那一字不改地展开给人看。"""

    code: str
    name: str = ""
    category: str = ""
    size: str = ""
    format: str = ""
    file_name: str = ""
    description: str = ""
    anchors: str = ""
    constraints: list[str] = Field(default_factory=list)
    prompt: str = ""
    negative_prompt: str = ""
    card: str = ""


class AssetSpecIn(Schema):
    note: str = Field(default="", max_length=2000, description="不满意哪里，空着就是头一版")
    field: str = Field(
        default="", max_length=64, description="只改这一项（如 prompt / 尺寸），空着就是整张重做"
    )


class RenderIn(AssetSpecIn):
    pass


class GenerationOut(Schema):
    """一条产物台账。`is_final` 为真就是人采用的那一张。"""

    id: str
    stage: str
    variant: str | None = None
    file_path: str
    file_hash: str | None = None
    is_final: bool
    created_at: str
    asset_spec: dict[str, Any] = Field(default_factory=dict)


class RenderOut(Schema):
    character_id: str
    generation_id: str
    file_path: str
    """落在 `tmp/` 里的候选位。定稿位要等人采用之后才有。"""
    width: int
    height: int
    spec: AssetSpecOut
    params: dict[str, Any] = Field(default_factory=dict)
    """生效参数快照：模型、请求尺寸与实际尺寸、耗时。可复现靠它。"""


class RenderAdoptIn(GateIn):
    generation_id: str = Field(min_length=1, description="要采用的那一张")


# --------------------------------------------------------------------------- #
# 四视图
# --------------------------------------------------------------------------- #


class ViewsIn(Schema):
    variants: list[str] = Field(
        default_factory=list,
        description="兼容旧客户端；新版四视图只能整张生成，必须留空",
    )
    seed: int | None = Field(default=None, description="整张四宫格的随机种子")


class ViewImageOut(Schema):
    """单张四视图四宫格候选。"""

    variant: str
    label: str
    generation_id: str
    file_path: str
    """落在 `tmp/` 里的候选位。定稿位要等人选完输入才有。"""

    width: int
    height: int
    problems: list[str] = Field(default_factory=list)
    """机器量出来的问题（背景不纯、尺寸不对）。不为空也照样给图：判定归评审与人工。"""

    params: dict[str, Any] = Field(default_factory=dict)


class ViewFailureOut(Schema):
    variant: str
    label: str
    reason: str


class ViewSetOut(Schema):
    character_id: str
    state: str
    state_label: str
    images: list[ViewImageOut] = Field(default_factory=list)
    failures: list[ViewFailureOut] = Field(default_factory=list)
    """生成失败时固定为 `sheet`；整张失败需整张重生。"""

    references: list[str] = Field(default_factory=list)
    """两张参考图：姿势模版与定稿渲染图。"""

    size_complaint: str | None = None
    ok: bool


class ViewReviewIn(Schema):
    mode: Literal["full", "lean", "solo"] | None = Field(
        default=None, description="盖掉项目的 review_mode，只对这一次生效"
    )
    regenerate: bool = Field(
        default=False, description="REJECT 时要不要自动重生整张四宫格（要花额度）"
    )


class ViewVerdictOut(Schema):
    variants: list[str]
    decision: str
    sections: dict[str, list[str]] = Field(default_factory=dict)
    text: str
    """裁决全文。卡片上原样展示——摘一句话用户判断不了该不该定稿。"""


class ViewReviewOut(Schema):
    character_id: str
    mode: str
    decision: str
    """整批结论，取最严那一档。`solo` 没审就是空串。"""

    approved: bool
    attempt: int
    regenerated: int
    manual: bool
    skipped: bool
    verdicts: list[ViewVerdictOut] = Field(default_factory=list)


class ViewsAdoptIn(GateIn):
    picks: dict[str, str] = Field(description='固定为 `{"sheet": generation_id}`')


class AdvanceIn(Schema):
    state: str = Field(min_length=1)


class TaskEventOut(Schema):
    seq: int
    ts: str
    level: str
    event: str
    message: str
    payload: dict[str, Any]


# --------------------------------------------------------------------------- #
# 会话与记忆
# --------------------------------------------------------------------------- #

TargetKind = Literal["project", "character"]
MemoryKind = Literal["preference", "taboo", "fact"]


class ConversationCreateIn(Schema):
    agent_code: str = Field(min_length=1)
    target_kind: TargetKind
    target_ref: str | None = Field(
        default=None, description="角色会话必填，填角色 id；项目会话留空"
    )
    title: str = Field(default="", max_length=255)


class ConversationOut(Schema):
    id: str
    target_kind: str
    target_ref: str | None = None
    agent_code: str
    title: str
    status: str
    bound_provider_label: str = ""
    rebind_count: int = 0
    rebind_reason: str | None = None
    created_at: str
    updated_at: str
    message_count: int = 0
    pending_drafts: int = 0


class MessageOut(Schema):
    id: int
    turn_no: int
    role: str
    content: str
    token_count: int = 0
    folded: bool = False
    """已折进摘要。原文仍在这里，前端默认收起、点开可看。"""
    status: str = "done"
    """thinking=正在等回答（前端摆转圈与中断按钮）、done、failed、cancelled。"""
    agent_code: str = ""
    """这句话的实际发送 Agent；用户消息固定为 user。"""
    recipient_agent_code: str = ""
    """用户消息的实际收件 Agent；assistant 消息通常为空。"""
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    """这条消息带的非文字产物，元素形如 `{"kind": "image", "path": ..., "generation_id": ...}`。"""
    created_at: str


class ConversationMemoryOut(Schema):
    summary: str = ""
    decisions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    folded_turns: int = 0


class DraftOut(Schema):
    id: str
    target_path: str
    content: str
    based_on_hash: str = ""
    status: str
    created_at: str
    stale: bool = False
    """基线已经变了：草稿写出来之后定稿被别处改过，此时沉淀会被拒绝。"""


class NamingOptionOut(Schema):
    """一条项目命名建议。`code` 可能为空（模型给的代号不合法），前端要允许用户自己补。"""

    name: str
    code: str = ""
    reason: str = ""


class ChoiceGroupOut(Schema):
    """一处要用户拍板的分歧。前端摆成选择组件，用户点完拼成一句话发回去。"""

    item: str
    options: list[str] = Field(default_factory=list)
    recommended: list[str] = Field(default_factory=list)
    """Agent 的推荐，前端拿它预选。空就是没给或给的不在选项里；单选题最多一个。"""
    multiple: bool = False
    """真则这一项能同时拍好几个值，由 Agent 自己判断。"""


class AgentOptionOut(Schema):
    agent_code: str
    role: str
    role_type: str
    capability: str
    focusable: bool
    aliases: list[str] = Field(default_factory=list)


class HandoffOut(Schema):
    turn_no: int
    from_agent_code: str
    to_agent_code: str
    source: str
    reason: str
    status: str
    created_at: str | None = None


class ConversationDetailOut(Schema):
    conversation: ConversationOut
    director_agent_code: str = "studio_director"
    focus_agent_code: str | None = None
    focus_reason: str | None = None
    available_agents: list[AgentOptionOut] = Field(default_factory=list)
    handoffs: list[HandoffOut] = Field(default_factory=list)
    messages: list[MessageOut] = Field(default_factory=list)
    memory: ConversationMemoryOut = Field(default_factory=ConversationMemoryOut)
    drafts: list[DraftOut] = Field(default_factory=list)
    artifact_path: str | None = None
    """这场会话在改哪个定稿文件，供前端 diff 面板标题使用。"""
    naming: list[NamingOptionOut] = Field(default_factory=list)
    """最近一轮给的命名建议，立项收口面板拿它做候选项。落盘之前一律为空。"""
    settled: bool = False
    """真则这场会话已经落过盘。立项页拿它分两段：假则还在对焦风格，真了才轮到定项目名。"""
    choices: list[ChoiceGroupOut] = Field(default_factory=list)
    """最近一轮要用户拍板的选项，前端摆在输入框上方。"""
    action: str = ""
    target_agent: str | None = None
    reason: str = ""
    briefing: str = ""
    """摆在消息最前面的开场提示：项目现状与接下来该说什么。平台现算，不入库、不进上下文，
    只项目会话有。"""
    briefing_blank: bool = False
    """真则这只是一句开场号召（项目还是白纸），前端把它居中铺成大字而不是当成对话气泡。"""


class SendMessageIn(Schema):
    content: str = Field(min_length=1)
    stream: bool = Field(default=True, description="是否往 SSE 通道推增量")
    recipient_agent_code: str | None = Field(
        default=None, description="显式指定本轮收件 Agent；未填时按焦点和总管规则路由"
    )


class TurnOut(Schema):
    conversation_id: str
    turn_no: int
    content: str
    draft_ids: list[str] = Field(default_factory=list)
    folded_turns: list[int] = Field(default_factory=list)
    context_tokens: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    provider_label: str = ""
    agent_code: str = ""
    focus_agent_code: str | None = None
    handoffs: list[HandoffOut] = Field(default_factory=list)
    naming: list[NamingOptionOut] = Field(default_factory=list)
    choices: list[ChoiceGroupOut] = Field(default_factory=list)
    action: str = ""
    target_agent: str | None = None
    reason: str = ""


class CommitIn(Schema):
    draft_ids: list[str] | None = Field(default=None, description="留空即沉淀全部待确认草稿")
    continue_pipeline: bool = Field(
        default=False,
        description="角色设定沉淀后是否立即继续生成首版效果图",
    )


class ArchivedOut(Schema):
    target_path: str
    content_hash: str
    previous_path: str | None = None
    """旧定稿退位后的位置，都在同级 `tmp/` 下。"""


class CommitOut(Schema):
    conversation_id: str
    archived: list[ArchivedOut] = Field(default_factory=list)
    memories_added: list[str] = Field(default_factory=list)


class DiscardOut(Schema):
    conversation_id: str
    discarded: int


class InterruptOut(Schema):
    conversation_id: str
    interrupted: bool
    """假就是本来就没在跑（早回完了、或别处已经中断过）。"""


class DiffOut(Schema):
    target_path: str
    current: str
    draft: str
    stale: bool = False
    warnings: list[str] = Field(default_factory=list)
    """沉下去之前该知道的事（art bible 还空着的节、不会生效的配置键），不拦只提醒。"""


class ProjectMemoryOut(Schema):
    id: str
    """内容哈希，不是主键：条目存在 Markdown 里，改了内容 id 就变了。"""
    kind: str
    content: str
    character_ref: str = ""
    """空串是项目级（注入所有会话），否则只注入这个角色的会话。"""
    enabled: bool = True


class ProjectMemoryIn(Schema):
    kind: MemoryKind
    content: str = Field(min_length=1)
    character_ref: str = Field(default="", description="只给某个角色用就填角色 id；留空是项目级")


class ProjectMemoryPatch(Schema):
    content: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None
