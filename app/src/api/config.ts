import { request, withQuery } from './client'
import type { AgentDef, CatalogEntry, Health, Options } from '@/types/api'

/** 下拉框取值域。配置库灌完就不变，缓存久一点没风险。 */
export function options(): Promise<Options> {
  return request<Options>('/api/config/options')
}

/** Agent 定义来自 `prompts/agents/*.md`，是代码资产，UI 只读。 */
export function agents(includePrompt = false): Promise<AgentDef[]> {
  return request<AgentDef[]>(
    withQuery('/api/config/agents', { include_prompt: includePrompt || null }),
  )
}

/** 型号速查表：新建模型时照着填 driver / api_path，少抄错。 */
export function modelCatalog(params: { vendor?: string; capability?: string } = {}) {
  return request<CatalogEntry[]>(withQuery('/api/config/model-catalog', params))
}

export function health(): Promise<Health> {
  return request<Health>('/api/health')
}
