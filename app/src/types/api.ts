/** 后端 `api/schemas.py` 与 `api/config.py` 响应模型的镜像。字段名一律与后端保持一致。 */

export type LimitKind = 'tokens' | 'calls' | 'credits'

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
  /** 当前项目的 code；一台新装的机器上还没有项目时是 null。 */
  current_project: string | null
  /** 当前项目自带的库文件，跟着项目目录走。 */
  project_db: string | null
}

// --------------------------------------------------------------------------- //
// 项目
// --------------------------------------------------------------------------- //

/** 装机项目列表的一行。带绝对路径：项目可以在磁盘任意位置，用户靠它分辨同名项目。 */
export interface ProjectSummary {
  code: string
  name: string
  dir_path: string
  /** 在默认项目根下（平台替它管目录），否则只是挂着。 */
  managed: boolean
  /** 目录当下不在（外置盘没挂、被搬走），列表里还留着但不能用。 */
  missing: boolean
  is_current: boolean
  last_opened_at: string | null
}

export interface ProjectList {
  projects: ProjectSummary[]
  current: string | null
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
}

/** 配置表单的提交体。code 是跟着目录走的身份，改不了，所以不在这里。 */
export interface ProjectConfigPatch {
  name?: string
  style?: ProjectStyle
  defaults?: ProjectDefaults
  pose_template?: string | null
  review_mode?: ReviewMode
}

export interface ProjectCreateIn {
  name: string
  code: string
  /** 留空建在默认项目根下，给了就建在这个任意位置。 */
  dir_path?: string | null
  style?: Partial<ProjectStyle>
  review_mode?: ReviewMode
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

export interface Character {
  id: string
  name: string
  /** 相对项目目录的路径，如 `characters/chitong_beast`。 */
  dir_name: string
  state: string
  spec_path: string | null
  updated_at: string
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

export interface ConversationDetail {
  conversation: Conversation
  messages: Message[]
  memory: ConversationMemory
  drafts: Draft[]
  /** 这场会话在改哪个定稿文件，diff 面板拿它做标题。 */
  artifact_path: string | null
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
  /** 停用是让它不再注入上下文，不是删掉。 */
  enabled: boolean
  source_conversation_id: string | null
  created_at: string
}
