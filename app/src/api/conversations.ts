/**
 * 会话接口。
 *
 * 两条通道要分清楚：`POST /messages` 是同步的，等模型答完才返回，返回的那份才是落库的
 * 真相；`GET /stream` 只是让字一个个出现。所以面板的做法是先订流、再发消息，流断了也
 * 不影响这一轮的结果——刷一次详情就有。
 *
 * 沉淀（`POST /commit`）是整条链路里唯一会改磁盘的动作，其余接口都只动库。
 */
import { baseUrl, request, withQuery } from './client'
import type {
  CommitResult,
  Conversation,
  ConversationDetail,
  Diff,
  DiscardResult,
  InterruptResult,
  MemoryKind,
  ProjectMemoryItem,
  TargetKind,
  Turn,
} from '@/types/api'

export interface ConversationQuery {
  targetKind?: TargetKind
  targetRef?: string
  limit?: number
}

export function listConversations(query: ConversationQuery = {}): Promise<Conversation[]> {
  return request<Conversation[]>(
    withQuery('/api/conversations', {
      target_kind: query.targetKind,
      target_ref: query.targetRef,
      limit: query.limit,
    }),
  )
}

export interface StartConversationIn {
  agent_code: string
  target_kind: TargetKind
  target_ref?: string | null
  title?: string
}

export function startConversation(payload: StartConversationIn): Promise<ConversationDetail> {
  return request<ConversationDetail>('/api/conversations', { method: 'POST', body: payload })
}

/**
 * 拿这个对焦对象当下该聊的会话，没有就开一场。
 *
 * 幂等，可以当普通查询用：只有上一场已经沉淀或丢弃时才真的新建。
 */
export function ensureConversation(payload: StartConversationIn): Promise<ConversationDetail> {
  return request<ConversationDetail>('/api/conversations/ensure', {
    method: 'POST',
    body: payload,
  })
}

export function readConversation(id: string): Promise<ConversationDetail> {
  return request<ConversationDetail>(`/api/conversations/${encodeURIComponent(id)}`)
}

/** 慢是应该的：用户按了发送就在等这一轮的结果，这里不做超时。 */
export function sendMessage(id: string, content: string, stream = true): Promise<Turn> {
  return request<Turn>(`/api/conversations/${encodeURIComponent(id)}/messages`, {
    method: 'POST',
    body: { content, stream },
  })
}

export function readDiff(id: string, draftId: string): Promise<Diff> {
  const path = `/api/conversations/${encodeURIComponent(id)}/drafts/${encodeURIComponent(draftId)}/diff`
  return request<Diff>(path)
}

/** 不传 draftIds 就是沉淀全部待确认草稿。基线过期时后端返回 409，磁盘一个字节不动。 */
export function commitConversation(id: string, draftIds?: string[]): Promise<CommitResult> {
  return request<CommitResult>(`/api/conversations/${encodeURIComponent(id)}/commit`, {
    method: 'POST',
    body: { draft_ids: draftIds ?? null },
  })
}

export function discardConversation(id: string): Promise<DiscardResult> {
  return request<DiscardResult>(`/api/conversations/${encodeURIComponent(id)}/discard`, {
    method: 'POST',
  })
}

/**
 * 中断正在跑的那一轮：库里那条「正在想」改成取消，推理还在就叫停。
 *
 * 重启后卡住的那种也走这一口：叫停已经无人可叫，但状态总得能清。
 */
export function interruptConversation(id: string): Promise<InterruptResult> {
  return request<InterruptResult>(`/api/conversations/${encodeURIComponent(id)}/interrupt`, {
    method: 'POST',
  })
}

// --------------------------------------------------------------------------- //
// 项目长期记忆
// --------------------------------------------------------------------------- //

export function listMemories(): Promise<ProjectMemoryItem[]> {
  return request<ProjectMemoryItem[]>('/api/memory')
}

export function addMemory(kind: MemoryKind, content: string): Promise<ProjectMemoryItem> {
  return request<ProjectMemoryItem>('/api/memory', { method: 'POST', body: { kind, content } })
}

export function patchMemory(
  id: string,
  patch: { content?: string; enabled?: boolean },
): Promise<ProjectMemoryItem> {
  return request<ProjectMemoryItem>(`/api/memory/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: patch,
  })
}

export function deleteMemory(id: string): Promise<void> {
  return request<void>(`/api/memory/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

// --------------------------------------------------------------------------- //
// 生成增量（SSE）
// --------------------------------------------------------------------------- //

export interface ConversationSubscription {
  conversationId: string
  /** 断线重连或换会话时接着看：不给就从缓冲里还留着的那段开始。 */
  afterSeq?: number
  /**
   * 只要订上来之后新产生的字。
   *
   * 刚发完一轮就得带上：这条流跟发消息那个请求几乎同时出发，赶在后端清缓冲之前到的话，
   * 从缓冲头读到的就是上一轮的整段回答，而它末尾的 `turn` 还会把流当场收掉。
   */
  fresh?: boolean
  onDelta: (piece: string) => void
  /** 一轮有了结果，后端推完这条就收流。 */
  onTurn?: (turn: { turn_no: number; drafts: string[] }) => void
  onError?: (reason: string) => void
}

/**
 * 订阅一场会话的生成增量，返回退订函数。
 *
 * 后端在一轮有了结果（turn / error）之后自己收流，EventSource 会以为断线并自动重连，
 * 所以拿到终态就主动关掉——重连回来只会把同一批增量再推一遍。
 */
export function subscribeConversation(sub: ConversationSubscription): () => void {
  let source: EventSource | null = null
  let cancelled = false

  void baseUrl().then((base) => {
    if (cancelled) return
    const path = withQuery(`/api/conversations/${encodeURIComponent(sub.conversationId)}/stream`, {
      after_seq: sub.afterSeq,
      fresh: sub.fresh === true ? 1 : undefined,
    })
    source = new EventSource(`${base}${path}`)
    source.addEventListener('delta', (event) => {
      sub.onDelta((event as MessageEvent<string>).data)
    })
    source.addEventListener('turn', (event) => {
      sub.onTurn?.(
        JSON.parse((event as MessageEvent<string>).data) as { turn_no: number; drafts: string[] },
      )
      source?.close()
    })
    source.addEventListener('error', (event) => {
      // 后端推的失败事件带 data，浏览器自己的连接错误不带——只有前者才是「这一轮炸了」
      const data = (event as MessageEvent<string>).data
      if (data === undefined) return
      sub.onError?.(data)
      source?.close()
    })
  })

  return () => {
    cancelled = true
    source?.close()
  }
}
