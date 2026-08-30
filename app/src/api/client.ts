/**
 * 后端地址的唯一出处。
 *
 * 在 Electron 里端口是运行时才知道的（主进程从后端 stdout 读来），所以这里缓存一次 Promise，
 * 后续所有请求共用；脱离 Electron 直接开浏览器（`npm run dev:web`）时退到
 * `VITE_API_PORT`，没配就按约定的 8799——单独起后端就是 `atelier-serve --port 8799`。
 */

/** 单独跑后端时的约定端口。前端配置得能改，但默认值要跟后端文档里那条命令对上。 */
export const DEFAULT_DEV_PORT = 8799

let cached: Promise<string> | null = null

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export class BackendUnavailable extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'BackendUnavailable'
  }
}

/** 端口 + 前缀拼成基址；单独抽出来是为了能直接测。 */
export function joinBase(port: number): string {
  return `http://127.0.0.1:${port}`
}

export function resetBaseUrl(): void {
  cached = null
}

export function baseUrl(): Promise<string> {
  cached ??= resolveBaseUrl()
  return cached
}

async function resolveBaseUrl(): Promise<string> {
  const bridge = window.atelier
  if (!bridge) {
    const configured = import.meta.env.VITE_API_PORT
    return joinBase(configured ? Number(configured) : DEFAULT_DEV_PORT)
  }
  const port = await bridge.port()
  if (port === null) {
    const reason = await bridge.startupError()
    // 端口拿不到就一次都别缓存，后端起来后还能再试
    cached = null
    throw new BackendUnavailable(reason ?? '后端还没起来')
  }
  return joinBase(port)
}

/** 拼查询串：null / undefined / 空串一概不带，免得后端收到 "" 当成有效值。 */
export function withQuery(path: string, params: Record<string, unknown> = {}): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue
    search.append(key, String(value))
  }
  const query = search.toString()
  return query ? `${path}?${query}` : path
}

/** 后端错误统一是 {"detail": "..."}，取出来当 message，取不到就退回状态码。 */
export async function errorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json()
    if (body && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail: unknown }).detail
      return typeof detail === 'string' ? detail : JSON.stringify(detail)
    }
  } catch {
    // 响应不是 JSON（例如 502 网关页），下面用状态码兜底
  }
  return `请求失败（HTTP ${response.status}）`
}

interface RequestInitLike {
  method?: string
  body?: unknown
}

export async function request<T>(path: string, init: RequestInitLike = {}): Promise<T> {
  const base = await baseUrl()
  const response = await fetch(`${base}${path}`, {
    method: init.method ?? 'GET',
    headers: init.body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  })
  if (!response.ok) throw new ApiError(response.status, await errorMessage(response))
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}
