/**
 * SSE 订阅。用浏览器原生 EventSource——它自带断线重连，比手搓 fetch 流省事。
 *
 * 后端的 route-logs 是永不结束的流，所以退订必须由调用方显式做（返回的函数）。
 */
import { baseUrl, withQuery } from './client'
import type { RouteLog } from '@/types/api'

export interface RouteLogSubscription {
  onLog: (log: RouteLog) => void
  onError?: (error: Event) => void
  agentCode?: string
  afterId?: number
}

/** 返回退订函数。连接是异步建的，退订可能早于连上，所以要记一个「已取消」标志。 */
export function subscribeRouteLogs(sub: RouteLogSubscription): () => void {
  let source: EventSource | null = null
  let cancelled = false

  void baseUrl().then((base) => {
    if (cancelled) return
    const path = withQuery('/api/events/route-logs', {
      agent_code: sub.agentCode,
      after_id: sub.afterId,
    })
    source = new EventSource(`${base}${path}`)
    source.addEventListener('route_log', (event) => {
      sub.onLog(JSON.parse((event as MessageEvent<string>).data) as RouteLog)
    })
    if (sub.onError) source.addEventListener('error', sub.onError)
  })

  return () => {
    cancelled = true
    source?.close()
  }
}

export interface TaskEventPayload {
  seq: number
  event: string
  message: string
  payload: Record<string, unknown>
}

export interface TaskSubscription {
  taskId: string
  afterSeq?: number
  onEvent: (event: TaskEventPayload) => void
  onRouteLog?: (log: RouteLog) => void
  /** 任务进终态时后端推 done 并收流，这里顺手帮调用方关掉连接。 */
  onDone?: (status: string) => void
  onError?: (error: Event) => void
}

export function subscribeTask(sub: TaskSubscription): () => void {
  let source: EventSource | null = null
  let cancelled = false

  void baseUrl().then((base) => {
    if (cancelled) return
    const path = withQuery(`/api/events/${encodeURIComponent(sub.taskId)}`, {
      after_seq: sub.afterSeq,
    })
    source = new EventSource(`${base}${path}`)
    source.addEventListener('task_event', (event) => {
      sub.onEvent(JSON.parse((event as MessageEvent<string>).data) as TaskEventPayload)
    })
    source.addEventListener('route_log', (event) => {
      sub.onRouteLog?.(JSON.parse((event as MessageEvent<string>).data) as RouteLog)
    })
    source.addEventListener('done', (event) => {
      sub.onDone?.((event as MessageEvent<string>).data)
      source?.close()
    })
    if (sub.onError) source.addEventListener('error', sub.onError)
  })

  return () => {
    cancelled = true
    source?.close()
  }
}
