/**
 * 角色接口的调用形状。
 *
 * 这里钉的是几个容易走形的前后端约定：评审与门禁是两个不同的动作（前端不许拿 `approved` 自动
 * 去 confirm）、驳回后自动重生要带上会话 id、角色 id 进路径要转义。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  advanceCharacter,
  confirmSpec,
  createCharacter,
  listCharacterEvents,
  readCharacter,
  rejectSpec,
  reviewSpec,
} from './characters'
import { resetBaseUrl } from './client'

interface Call {
  url: string
  method: string
  body: unknown
}

let calls: Call[] = []

beforeEach(() => {
  calls = []
  ;(globalThis as { window?: unknown }).window = {
    atelier: {
      port: () => Promise.resolve(62066),
      startupError: () => Promise.resolve(null),
      logBacklog: () => Promise.resolve([]),
      onBackendLog: () => () => undefined,
      chooseDirectory: () => Promise.resolve(null),
    },
  }
  vi.stubGlobal('fetch', (url: string, init?: { method?: string; body?: string }) => {
    calls.push({
      url,
      method: init?.method ?? 'GET',
      body: init?.body === undefined ? undefined : JSON.parse(init.body),
    })
    return Promise.resolve(new Response('{}', { status: 200 }))
  })
})

afterEach(() => {
  resetBaseUrl()
  vi.unstubAllGlobals()
  delete (globalThis as { window?: unknown }).window
})

function onlyCall(): Call {
  expect(calls).toHaveLength(1)
  const [call] = calls
  if (!call) throw new Error('一个请求都没发出去')
  return call
}

describe('建角色与读取', () => {
  it('名字走请求体，目录名由后端定', async () => {
    await createCharacter('赤瞳')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/characters',
      method: 'POST',
      body: { name: '赤瞳' },
    })
  })

  it('id 进路径要转义，免得带斜杠时把路径拼歪', async () => {
    await readCharacter('a/b')
    expect(onlyCall().url).toBe('http://127.0.0.1:62066/api/characters/a%2Fb')
  })

  it('事件时间线是只读的', async () => {
    await listCharacterEvents('c1')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/characters/c1/events',
      method: 'GET',
    })
  })
})

describe('评审', () => {
  it('不带会话就是只审一次，不自动重生', async () => {
    await reviewSpec('c1')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/characters/c1/review',
      method: 'POST',
      body: { conversation_id: null },
    })
  })

  it('要自动重生就得给出承载那几轮的会话', async () => {
    await reviewSpec('c1', 'conv-9')
    expect(onlyCall().body).toEqual({ conversation_id: 'conv-9' })
  })
})

describe('门禁 1', () => {
  it('确认与驳回是两个不同的端点，不靠一个参数区分', async () => {
    await confirmSpec('c1', '看过了')
    await rejectSpec('c1', '环境设定还没写')

    expect(calls.map((one) => one.url)).toEqual([
      'http://127.0.0.1:62066/api/characters/c1/spec/confirm',
      'http://127.0.0.1:62066/api/characters/c1/spec/reject',
    ])
    expect(calls.map((one) => one.body)).toEqual([{ note: '看过了' }, { note: '环境设定还没写' }])
  })

  it('推进状态得说清推到哪一步，不做「下一步」这种隐式跳转', async () => {
    await advanceCharacter('c1', 'S2_render_generated')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/characters/c1/advance',
      method: 'POST',
      body: { state: 'S2_render_generated' },
    })
  })
})
