/**
 * 角色接口。
 *
 * 页面上要分清两件事，接口也是分开的：`review` 只给裁决与理由，`spec/confirm` 才是放行。
 * 哪怕裁决是 `APPROVE` 也得人按一下——所以前端不许拿 `approved` 自动去调 confirm。
 *
 * 评审慢是应该的（一次调用加上最多三轮自动重生），这里不设超时：用户按了「评审」就在等这一
 * 轮的结果。
 */
import { request } from './client'
import type { Character, SpecReview, TaskEvent } from '@/types/api'

export function createCharacter(name: string): Promise<Character> {
  return request<Character>('/api/characters', { method: 'POST', body: { name } })
}

export function readCharacter(id: string): Promise<Character> {
  return request<Character>(`/api/characters/${encodeURIComponent(id)}`)
}

export function listCharacterEvents(id: string): Promise<TaskEvent[]> {
  return request<TaskEvent[]>(`/api/characters/${encodeURIComponent(id)}/events`)
}

/**
 * 让 `spec_reviewer` 审最新那一版设定。
 *
 * 带上会话 id 才会在 `REJECT` 后自动把理由发回写手重生——重生得有个会话承载新的一轮，不然
 * 那几轮对话跟用户自己那场对不上号。
 */
export function reviewSpec(id: string, conversationId?: string | null): Promise<SpecReview> {
  return request<SpecReview>(`/api/characters/${encodeURIComponent(id)}/review`, {
    method: 'POST',
    body: { conversation_id: conversationId ?? null },
  })
}

/** 门禁 1：人工确认设定。确认的是磁盘上那一份，没沉淀过后端会 409。 */
export function confirmSpec(id: string, note: string): Promise<Character> {
  return request<Character>(`/api/characters/${encodeURIComponent(id)}/spec/confirm`, {
    method: 'POST',
    body: { note },
  })
}

/** 门禁 1 驳回：状态停在原地，理由记进时间线，下一轮设定会话能看见上次为什么没过。 */
export function rejectSpec(id: string, note: string): Promise<Character> {
  return request<Character>(`/api/characters/${encodeURIComponent(id)}/spec/reject`, {
    method: 'POST',
    body: { note },
  })
}

/** 推进状态。只能往前且一步一步走，没过门禁会拿到 409。 */
export function advanceCharacter(id: string, state: string): Promise<Character> {
  return request<Character>(`/api/characters/${encodeURIComponent(id)}/advance`, {
    method: 'POST',
    body: { state },
  })
}
