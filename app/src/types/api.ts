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
}
