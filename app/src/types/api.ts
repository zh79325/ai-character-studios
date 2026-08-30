/** 后端 `api/schemas.py` 与 `api/config.py` 响应模型的镜像。字段名一律与后端保持一致。 */

export type LimitKind = 'tokens' | 'calls' | 'credits' | 'images'

export interface LimitIn {
  limit_kind: string
  /** 0 或不配 = 不限量。 */
  max_value: number
  group_name: string
  period_expr: string
}

export interface LimitOut extends LimitIn {
  id: number
  window_text: string
}

export interface Budget {
  limit_kind: string
  limit: number
  used: number
  remaining: number | null
  available: number | null
  window_key: string
  window_text: string
  period_expr: string
  group_name: string
  source: string
  exhausted: boolean
  unlimited: boolean
}

export interface Breaker {
  open_until: string
  fail_count: number
  last_reason: string | null
}

export interface ModelIn {
  model_id: string
  capabilities: string[]
  driver?: string | null
  api_path?: string | null
  enabled: boolean
  sort_no: number
  params: Record<string, unknown>
  remark?: string | null
  agents: string[]
  limits: LimitIn[]
}

export interface Model {
  id: number
  provider_code: string
  model_id: string
  capabilities: string[]
  driver: string | null
  effective_driver: string
  api_path: string | null
  endpoint: string
  enabled: boolean
  sort_no: number
  params: Record<string, unknown>
  remark: string | null
  agents: string[]
  limits: LimitOut[]
}

export interface ProviderIn {
  code: string
  name: string
  base_url: string
  /** 空串表示不带 key；响应里永远看不到明文。 */
  api_key: string
  enabled: boolean
  priority: number
  driver: string
  auth_style: 'bearer' | 'x-api-key'
  verify_ssl: boolean
  remark?: string | null
  models: ModelIn[]
}

/** PATCH 只改传进来的字段：api_key 传空串是清空，不传是不动。 */
export type ProviderPatch = Partial<Omit<ProviderIn, 'code' | 'models'>>

/** 预设里的一个模型。额度数字没有默认值，套餐买了多少只有用户自己知道。 */
export interface PresetModel {
  model_id: string
  capabilities: string[]
  driver: string
  api_path: string | null
  /** 这个模型按什么计量：`tokens` / `calls` / `credits`。 */
  limit_kind: string
  /** 建议的额度窗口，如 `day+11H`（每天 11 点重置）。 */
  default_period: string
  remark: string | null
}

/**
 * 一个套餐一份预设，来自配置库的型号目录。
 *
 * 只是新建表单的初值：选完仍走 `POST /api/providers`，所以预设错了当场能改。
 */
export interface ProviderPreset {
  /** 建议的账号标识；同一套餐开两个号得各起一个。 */
  code: string
  vendor: string
  plan: string
  /** 下拉里显示的那行字，如 `火山方舟 · Coding Plan`。 */
  label: string
  base_url: string
  driver: string
  auth_style: string
  /** key 的前缀提示；填错 key 往往不报错，只是不走套餐额度。 */
  key_prefix: string | null
  models: PresetModel[]
}

export interface Provider {
  code: string
  name: string
  base_url: string
  api_key_mask: string
  has_key: boolean
  enabled: boolean
  priority: number
  driver: string
  auth_style: string
  verify_ssl: boolean
  remark: string | null
  models: Model[]
}

export interface ModelUsage {
  provider_model_id: number
  provider_code: string
  provider_name: string
  provider_enabled: boolean
  model_id: string
  enabled: boolean
  has_key: boolean
  priority: number
  agents: string[]
  budgets: Budget[]
  breaker: Breaker | null
}

export interface UsageBoard {
  items: ModelUsage[]
  limit_kinds: string[]
}

export interface ImportResult {
  created: string[]
  updated: string[]
  removed: string[]
  models: number
  bindings: number
  limits: number
  warnings: string[]
}

export interface RouteLog {
  id: number
  ts: string
  agent_code: string
  provider_code: string | null
  model_id: string | null
  outcome: string
  reason: string | null
  attempt_no: number
  latency_ms: number | null
  used_delta: number | null
  limit_kind: string | null
  task_id: string | null
  conversation_id: string | null
  project_code: string | null
}

export interface Options {
  drivers: string[]
  limit_kinds: string[]
  auth_styles: string[]
  period_units: string[]
  period_examples: Record<string, string>
}

export interface AgentDef {
  agent_code: string
  capability: string
  role: string
  max_turns: number
  conversational: boolean
  memory_scope: string
  context_budget: number
  output_contract: string
  allow_tools: string[]
  source_file: string
  system_prompt?: string | null
}

export interface CatalogEntry {
  id: number
  vendor: string
  plan: string
  driver: string
  model_id: string
  capabilities: string[]
  base_url: string | null
  api_path: string | null
  auth_style: string
  key_prefix: string | null
  remark: string | null
}

export interface Health {
  ok: boolean
  config_db: string
  runtime_db: string
  usage_server: string | null
  /** 本次运行里打开的项目 code；后端只记在内存，重启就回到 null。 */
  opened_project: string | null
  /** 打开的项目自带的库文件，跟着项目目录走。 */
  project_db: string | null
}

// --------------------------------------------------------------------------- //
// 项目
// --------------------------------------------------------------------------- //

/** 项目阶段：`drafting` 还在对焦、名字与骨架都没定；`ready` 已立项。 */
export type ProjectStage = 'drafting' | 'ready'

/** 装机项目列表的一行。带绝对路径：项目可以在磁盘任意位置，用户靠它分辨同名项目。 */
export interface ProjectSummary {
  code: string
  name: string
  dir_path: string
  /** 在默认项目根下（平台替它管目录），否则只是挂着。 */
  managed: boolean
  /** 目录当下不在（外置盘没挂、被搬走），列表里还留着但不能用。 */
  missing: boolean
  last_opened_at: string | null
  stage: ProjectStage
}

export interface ProjectList {
  projects: ProjectSummary[]
  /** 本次运行里打开的项目代号。只存内存，后端重启就回到 null。 */
  opened: string | null
  /** 默认项目根，新建时拿它做目录预填。 */
  default_root: string
}

/** `project.json` 里的字段都允许用户手写额外键，所以这些结构都不是封闭的。 */
export interface ProjectStyle extends Record<string, unknown> {
  art_style: string
  mood: string
  palette: string
  quality: string
}

export interface ProjectDefaults extends Record<string, unknown> {
  image_size: number
  texture_resolution: string
  enable_pbr: boolean
  target_polycount: number
  pose_mode: string
  height_meters: number
}

export type ReviewMode = 'full' | 'lean' | 'solo'

export interface ProjectConfig extends Record<string, unknown> {
  code: string
  name: string
  style: ProjectStyle
  defaults: ProjectDefaults
  pose_template: string | null
  art_bible: string
  review_mode: ReviewMode
  stage: ProjectStage
}

/** 配置表单的提交体。code 是跟着目录走的身份，改不了，所以不在这里。 */
export interface ProjectConfigPatch {
  name?: string
  style?: ProjectStyle
  defaults?: ProjectDefaults
  pose_template?: string | null
  review_mode?: ReviewMode
}

/** 立项第一步：只占下目录，名字与代号等对焦完再定。 */
export interface ProjectBootstrapIn {
  dir_path: string
}

/** 立项收口：定下名字与代号，同时铺目录骨架与 git 规则。 */
export interface ProjectFinalizeIn {
  name: string
  code: string
}

export interface ArtBible {
  path: string
  content: string
  /** 「风格禁止项」一节抽出的条目，生图时拼进 negative_prompt。 */
  forbidden: string[]
}

export interface ScanResult {
  added: string[]
  /** 库里有而磁盘上没的素材：只报不删，目录可能只是还没拷过来。 */
  missing: string[]
  total: number
}

/** 从设定里抽出的一条硬性约束，如 `尾巴 = 2 条，彼此分离`。 */
export interface Constraint {
  item: string
  value: string
}

export interface Character {
  id: string
  name: string
  /** 相对项目目录的路径，如 `characters/chitong_beast`。 */
  dir_name: string
  state: string
  /** 状态的人话说法，直接显示；`state` 留给逻辑判断。 */
  state_label: string
  spec_path: string | null
  /** 人采用的那一张渲染图；null 表示门禁 2 还没过。候选在 `/renders` 里。 */
  render_path: string | null
  /** 定稿的四视图 `{视角: 相对路径}`。空对象表示还没定稿，建模吃的就是这四张。 */
  view_paths: Record<string, string>
  /** 评审抽出的约束清单，后续每一步生图都得守住它。 */
  hard_constraints: Constraint[]
  /** 人工门禁按下的时刻；null 表示这一关还没过。 */
  gate_spec_confirmed_at: string | null
  gate_render_confirmed_at: string | null
  updated_at: string
}

/** 一次设定评审的结果。裁决只是意见，放行仍要人按门禁。 */
export interface SpecReview {
  character_id: string
  /** `APPROVE` / `CONCERNS` / `REJECT`。 */
  decision: string
  approved: boolean
  /** 这是第几轮评审（含自动重生）。 */
  attempt: number
  regenerated: number
  /** 自动重生用尽仍没过：接下来只能人来判断。 */
  manual: boolean
  /** 分节理由，节名 → 条目。 */
  sections: Record<string, string[]>
  constraints: Constraint[]
  /** 裁决全文，原样展示——摘要过的理由会把判断依据丢掉。 */
  text: string
}

/** 角色身上发生过的事：裁决、门禁拍板与理由都在这条线上。 */
export interface TaskEvent {
  seq: number
  ts: string
  level: string
  event: string
  message: string
  payload: Record<string, unknown>
}

/**
 * 一张素材规格卡片。
 *
 * `card` 是模型写的原文，要一字不改展开给人看：分字段是平台按格式抽的，抽漏了的那句
 * 往往正是图不对的原因。
 */
export interface AssetSpec {
  code: string
  name: string
  category: string
  size: string
  format: string
  file_name: string
  description: string
  anchors: string
  constraints: string[]
  prompt: string
  negative_prompt: string
  card: string
}

/** 一条产物台账。`is_final` 为真就是人采用的那一张。 */
export interface Generation {
  id: string
  stage: string
  variant: string | null
  file_path: string
  file_hash: string | null
  is_final: boolean
  created_at: string
  /** 当时的卡片与参数快照，半年后想复现这张图靠它。 */
  asset_spec: Record<string, unknown>
}

/** 一次渲染图生成的结果。产物落在 `tmp/`，定稿位要等人按门禁。 */
export interface RenderResult {
  character_id: string
  generation_id: string
  file_path: string
  width: number
  height: number
  spec: AssetSpec
  params: Record<string, unknown>
}

/** 四视图里的一张。`problems` 是机器量出来的病，空数组就是白底与画幅都过了。 */
export interface ViewImage {
  /** `front` / `right` / `back` / `left`。定稿时要拿它当键。 */
  variant: string
  /** 视角的人话说法，直接显。 */
  label: string
  generation_id: string
  file_path: string
  width: number
  height: number
  problems: string[]
  /** 生效参数快照：模型、请求尺寸与实际尺寸。 */
  params: Record<string, unknown>
}

/** 没画出来的那一个视角。其他三张照旧留着，重生只重这一张。 */
export interface ViewFailure {
  variant: string
  label: string
  reason: string
}

/** 一批四视图的结果。四张齐了才会推到 S4。 */
export interface ViewSet {
  character_id: string
  state: string
  state_label: string
  images: ViewImage[]
  failures: ViewFailure[]
  /** 传进去的两张参考图：姿势模版在前，定稿渲染图在后。 */
  references: string[]
  /** 四张画幅不一致时的说明。不拦，但建模前得让人看见。 */
  size_complaint: string | null
  /** 四个角度都在且机器没量出问题。 */
  ok: boolean
}

/** 一次调用的看图裁决，以及它审的是哪几个视角。 */
export interface ViewVerdict {
  variants: string[]
  decision: string
  sections: Record<string, string[]>
  /** 裁决全文，原样展示。 */
  text: string
}

/** 一轮四视图评审的结果。裁决只能拦不能放行，定稿仍要人来选。 */
export interface ViewReview {
  character_id: string
  /** `full` 每张一次、`lean` 整批一次、`solo` 不审。 */
  mode: string
  decision: string
  approved: boolean
  attempt: number
  regenerated: number
  manual: boolean
  /** `solo` 模式根本没调用评审。 */
  skipped: boolean
  verdicts: ViewVerdict[]
}

// --------------------------------------------------------------------------- //
// 会话与记忆
// --------------------------------------------------------------------------- //

export type TargetKind = 'project' | 'character'
export type MemoryKind = 'preference' | 'taboo' | 'fact'

export interface Conversation {
  id: string
  target_kind: string
  target_ref: string | null
  agent_code: string
  title: string
  /** `active` / `committed` / `discarded`，后两种不能再发消息。 */
  status: string
  /** 这场会话粘在哪个候选上——多轮对话不换 provider，前缀缓存才用得上。 */
  bound_provider_label: string
  rebind_count: number
  rebind_reason: string | null
  created_at: string
  updated_at: string
  message_count: number
  pending_drafts: number
}

export interface Message {
  id: number
  turn_no: number
  role: string
  content: string
  token_count: number
  /** 已折进摘要。原文还在，面板默认收起、点开可看。 */
  folded: boolean
  created_at: string
}

export interface ConversationMemory {
  summary: string
  decisions: string[]
  open_questions: string[]
  folded_turns: number
}

export interface Draft {
  id: string
  target_path: string
  content: string
  based_on_hash: string
  status: string
  created_at: string
  /** 基线已经变了：这份草稿写出来之后定稿被别处改过，此时沉淀会被拒。 */
  stale: boolean
}

/** Agent 给的一组项目命名建议里的一条。用户在立项页选一条或自己重写。 */
export interface NamingOption {
  name: string
  /** 建议的代号；模型给得不合法时是空串，让用户自己填。 */
  code: string
  reason: string
}

export interface ConversationDetail {
  conversation: Conversation
  messages: Message[]
  memory: ConversationMemory
  drafts: Draft[]
  /** 这场会话在改哪个定稿文件，diff 面板拿它做标题。 */
  artifact_path: string | null
  /** 最近一轮给出的命名建议，没给过就是空。 */
  naming: NamingOption[]
}

export interface Turn {
  conversation_id: string
  turn_no: number
  content: string
  draft_ids: string[]
  /** 本轮被压进摘要的轮次，原文仍在库里。 */
  folded_turns: number[]
  context_tokens: number
  prompt_tokens: number | null
  completion_tokens: number | null
  provider_label: string
  naming: NamingOption[]
}

export interface Archived {
  target_path: string
  content_hash: string
  /** 旧定稿退位后的位置，都在同级 `tmp/` 下。 */
  previous_path: string | null
}

export interface CommitResult {
  conversation_id: string
  archived: Archived[]
  memories_added: string[]
}

export interface DiscardResult {
  conversation_id: string
  discarded: number
}

/** 两份全文，diff 怎么算、怎么显示都是前端的事。 */
export interface Diff {
  target_path: string
  current: string
  draft: string
  stale: boolean
  /** 这份草稿沉下去会留下的空洞或不生效之处；只是提醒，不拦沉淀。 */
  warnings: string[]
}

export interface ProjectMemoryItem {
  id: string
  kind: string
  content: string
  /** 空串是项目级（注入所有人），否则只跟着这一个角色。 */
  character_ref: string
  /** 停用是让它不再注入上下文，不是删掉。 */
  enabled: boolean
  source_conversation_id: string | null
  created_at: string
}
