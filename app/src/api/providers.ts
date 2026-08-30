import { request, withQuery } from './client'
import type {
  ImportResult,
  Model,
  ModelIn,
  Provider,
  ProviderIn,
  ProviderPatch,
  ProviderPreset,
  UsageBoard,
} from '@/types/api'

export function listProviders(): Promise<Provider[]> {
  return request<Provider[]>('/api/providers')
}

/**
 * 新建账号可选的套餐预设，来自配置库的型号目录。
 *
 * 只是表单初值，不建账号；它随 seeds 进 Git，不会因人而异，缓存久一点没风险。
 */
export function listPresets(): Promise<ProviderPreset[]> {
  return request<ProviderPreset[]>('/api/providers/presets')
}

export function createProvider(payload: ProviderIn): Promise<Provider> {
  return request<Provider>('/api/providers', { method: 'POST', body: payload })
}

export function patchProvider(code: string, patch: ProviderPatch): Promise<Provider> {
  return request<Provider>(`/api/providers/${encodeURIComponent(code)}`, {
    method: 'PATCH',
    body: patch,
  })
}

export function deleteProvider(code: string): Promise<void> {
  return request<void>(`/api/providers/${encodeURIComponent(code)}`, { method: 'DELETE' })
}

/** 同名模型算更新，设置页反复保存同一条不会撞冲突。 */
export function saveModel(code: string, payload: ModelIn): Promise<Model> {
  return request<Model>(`/api/providers/${encodeURIComponent(code)}/models`, {
    method: 'POST',
    body: payload,
  })
}

export function updateModel(code: string, modelPk: number, payload: ModelIn): Promise<Model> {
  return request<Model>(`/api/providers/${encodeURIComponent(code)}/models/${modelPk}`, {
    method: 'PUT',
    body: payload,
  })
}

export function deleteModel(code: string, modelPk: number): Promise<void> {
  return request<void>(`/api/providers/${encodeURIComponent(code)}/models/${modelPk}`, {
    method: 'DELETE',
  })
}

export function bindAgents(code: string, modelPk: number, agentCodes: string[]): Promise<Model> {
  return request<Model>(`/api/providers/${encodeURIComponent(code)}/models/${modelPk}/agents`, {
    method: 'PUT',
    body: agentCodes,
  })
}

export function usageBoard(refresh = false): Promise<UsageBoard> {
  return request<UsageBoard>(withQuery('/api/providers/usage', { refresh: refresh || null }))
}

export function clearBreaker(code: string, modelPk: number): Promise<void> {
  return request<void>(`/api/providers/${encodeURIComponent(code)}/models/${modelPk}/breaker`, {
    method: 'DELETE',
  })
}

export function resetUsage(
  code: string,
  modelPk: number,
  limitKind?: string,
): Promise<{ cleared: number }> {
  const path = withQuery(`/api/providers/${encodeURIComponent(code)}/models/${modelPk}/usage`, {
    limit_kind: limitKind,
  })
  return request<{ cleared: number }>(path, { method: 'DELETE' })
}

/** 默认导出的是不含 key 的模板；带 key 是用户主动做出的选择。 */
export function exportConfig(includeKeys: boolean): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(
    withQuery('/api/providers/export', { include_keys: includeKeys || null }),
  )
}

export function importConfig(
  providers: Record<string, unknown>,
  mode: 'merge' | 'replace',
): Promise<ImportResult> {
  return request<ImportResult>('/api/providers/import', {
    method: 'POST',
    body: { providers, mode },
  })
}
