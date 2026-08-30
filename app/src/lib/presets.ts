/**
 * 套餐预设 → 新建表单的一行行模型。
 *
 * 挑出来单独放：这几步决定了「用户只填一个额度数字」能不能变成一份正确的 provider 配置——
 * 额度留空要落成不限量而不是上限 0、能力对得上的 Agent 要先绑上（不绑就永远轮不到它）、
 * 取消勾选的模型不许建进去。放在抽屉组件里就只能靠手点验证。
 */
import type { AgentDef, ModelIn, ProviderPreset } from '@/types/api'

/** 预设带出来的一个模型：前几项是供应商定死的事实，后几项是用户这次要决定的。 */
export interface ModelRow {
  model_id: string
  capabilities: string[]
  driver: string
  api_path: string | null
  limit_kind: string
  remark: string | null
  /** 取消勾选就不建这个模型。 */
  picked: boolean
  /** 额度上限；留空或 0 就是不限量。 */
  max_value: number | null
  period_expr: string
  agents: string[]
}

/** 能力对得上的 Agent 全绑上。宁可绑多了让路由按优先级去挑，也别让模型永远闲着。 */
export function matchAgents(agents: AgentDef[], capabilities: string[]): string[] {
  return agents.filter((one) => capabilities.includes(one.capability)).map((one) => one.agent_code)
}

/** 把一份预设摊成表单行。额度不给默认值：拍一个不存在的上限比不限量更坑人。 */
export function rowsFromPreset(preset: ProviderPreset, agents: AgentDef[]): ModelRow[] {
  return preset.models.map((one) => ({
    model_id: one.model_id,
    capabilities: one.capabilities,
    driver: one.driver,
    api_path: one.api_path,
    limit_kind: one.limit_kind,
    remark: one.remark,
    picked: true,
    max_value: null,
    period_expr: one.default_period,
    agents: matchAgents(agents, one.capabilities),
  }))
}

/** 一行表单变成一个 ModelIn。额度留空就不发 limits，而不是发一条 max_value=0。 */
export function toModelIn(row: ModelRow): ModelIn {
  return {
    model_id: row.model_id,
    capabilities: row.capabilities,
    driver: row.driver,
    api_path: row.api_path,
    enabled: true,
    sort_no: 0,
    params: {},
    remark: row.remark,
    agents: row.agents,
    limits: row.max_value
      ? [
          {
            limit_kind: row.limit_kind,
            max_value: row.max_value,
            period_expr: row.period_expr,
            group_name: 'default',
          },
        ]
      : [],
  }
}

/** 勾上的那几行，按表单里的顺序。一个都没勾就建一个光账号，之后再加模型也行。 */
export function pickedModels(rows: ModelRow[]): ModelIn[] {
  return rows.filter((row) => row.picked).map(toModelIn)
}
