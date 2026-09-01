/**
 * 会话接口的调用形状。
 *
 * 这些断言钉的是前后端之间几处一走形就查不出来的约定：发消息必须带 `stream`（不带的话后端
 * 不推增量，面板会一直空着等）、沉淀不选草稿要显式给 `null`（给 `[]` 是「一份都不沉淀」，
 * 语义正好相反）、id 一律转义（会话 id 进路径，别处生成的 id 不保证是纯字母数字）、
 * 订流的 `after_seq` 只在真有游标时才带。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetBaseUrl } from './client'
import {
  addMemory,
  commitConversation,
  deleteMemory,
  discardConversation,
  listConversations,
  listMemories,
  patchMemory,
  readConversation,
  readDiff,
  sendMessage,
  startConversation,
  subscribeConversation,
} from './conversations'

interface Call {
  url: string
  method: string
  body: unknown
}

let calls: Call[] = []
const PROJECT = 'p one'

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

/** 断言只发了一个请求，并把它拿出来看。 */
function onlyCall(): Call {
  expect(calls).toHaveLength(1)
  const [call] = calls
  if (!call) throw new Error('一个请求都没发出去')
  return call
}

describe('列表与详情', () => {
  it('不给筛选条件就不拼查询串', async () => {
    await listConversations(PROJECT)
    expect(onlyCall().url).toBe('http://127.0.0.1:62066/api/projects/p%20one/conversations')
  })

  it('按对象筛选时 target_kind 与 target_ref 一起带上', async () => {
    await listConversations(PROJECT, { targetKind: 'character', targetRef: 'chitong', limit: 5 })
    expect(onlyCall().url).toBe(
      'http://127.0.0.1:62066/api/projects/p%20one/conversations?target_kind=character&target_ref=chitong&limit=5',
    )
  })

  it('新建会话把 agent 与对象放请求体，不走路径', async () => {
    await startConversation(PROJECT, { agent_code: 'game_designer', target_kind: 'project' })
    expect(onlyCall()).toEqual({
      url: 'http://127.0.0.1:62066/api/projects/p%20one/conversations',
      method: 'POST',
      body: { agent_code: 'game_designer', target_kind: 'project' },
    })
  })

  it('会话 id 进路径要转义', async () => {
    await readConversation(PROJECT, 'a/b')
    expect(onlyCall().url).toBe('http://127.0.0.1:62066/api/projects/p%20one/conversations/a%2Fb')
  })
})

describe('发消息', () => {
  it('默认要增量：不带 stream 后端就不推，面板只能干等', async () => {
    await sendMessage(PROJECT, 'c1', '拟一版')
    expect(onlyCall()).toEqual({
      url: 'http://127.0.0.1:62066/api/projects/p%20one/conversations/c1/messages',
      method: 'POST',
      body: { content: '拟一版', stream: true },
    })
  })

  it('不需要增量时显式关掉', async () => {
    await sendMessage(PROJECT, 'c1', '拟一版', false)
    expect(onlyCall().body).toEqual({ content: '拟一版', stream: false })
  })
})

describe('草稿与沉淀', () => {
  it('diff 的两个 id 都在路径上，都要转义', async () => {
    await readDiff(PROJECT, 'c 1', 'd/2')
    expect(onlyCall().url).toBe(
      'http://127.0.0.1:62066/api/projects/p%20one/conversations/c%201/drafts/d%2F2/diff',
    )
  })

  it('不选草稿就是全部沉淀，draft_ids 给 null——给空数组是一份都不沉淀', async () => {
    await commitConversation(PROJECT, 'c1')
    expect(onlyCall()).toEqual({
      url: 'http://127.0.0.1:62066/api/projects/p%20one/conversations/c1/commit',
      method: 'POST',
      body: { draft_ids: null, continue_pipeline: false },
    })
  })

  it('挑着沉淀就把选中的 id 原样送过去', async () => {
    await commitConversation(PROJECT, 'c1', ['d1', 'd2'])
    expect(onlyCall().body).toEqual({
      draft_ids: ['d1', 'd2'],
      continue_pipeline: false,
    })
  })

  it('确认继续时显式要求后端执行后续流水线', async () => {
    await commitConversation(PROJECT, 'c1', ['d1'], true)
    expect(onlyCall().body).toEqual({ draft_ids: ['d1'], continue_pipeline: true })
  })

  it('丢弃草稿是 POST，没有请求体', async () => {
    await discardConversation(PROJECT, 'c1')
    expect(onlyCall()).toEqual({
      url: 'http://127.0.0.1:62066/api/projects/p%20one/conversations/c1/discard',
      method: 'POST',
      body: undefined,
    })
  })
})

describe('项目记忆', () => {
  it('读的是当前项目的全量，含已停用的', async () => {
    await listMemories(PROJECT)
    expect(onlyCall().url).toBe('http://127.0.0.1:62066/api/projects/p%20one/memory')
  })

  it('角色页只请求项目级加当前角色的记忆', async () => {
    await listMemories(PROJECT, 'CHAR-孙悟空')
    expect(onlyCall().url).toBe(
      'http://127.0.0.1:62066/api/projects/p%20one/memory?character_ref=CHAR-%E5%AD%99%E6%82%9F%E7%A9%BA',
    )
  })

  it('新增带 kind', async () => {
    await addMemory(PROJECT, 'taboo', '不要露肩')
    expect(onlyCall().body).toEqual({ kind: 'taboo', content: '不要露肩' })
  })

  it('停用走 PATCH 而不是删除——停用只是不再注入', async () => {
    await patchMemory(PROJECT, 'm1', { enabled: false })
    expect(onlyCall()).toEqual({
      url: 'http://127.0.0.1:62066/api/projects/p%20one/memory/m1',
      method: 'PATCH',
      body: { enabled: false },
    })
  })

  it('删除是 DELETE', async () => {
    await deleteMemory(PROJECT, 'm1')
    expect(onlyCall().method).toBe('DELETE')
  })

  it('改完内容 id 会变——条目按内容哈希寻址，得拿响应里的新 id', async () => {
    vi.stubGlobal('fetch', () =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            id: 'hash-new',
            kind: 'preference',
            content: '尾巴要 2 条',
            character_ref: '',
            enabled: true,
          }),
          { status: 200 },
        ),
      ),
    )
    const updated = await patchMemory(PROJECT, 'hash-old', { content: '尾巴要 2 条' })
    expect(updated.id).toBe('hash-new')
  })
})

/** 记下建了哪些流、挂了哪些监听，好把事件喂回去。 */
class FakeEventSource {
  static made: FakeEventSource[] = []
  closed = false
  private readonly listeners = new Map<string, (event: MessageEvent<string>) => void>()

  constructor(readonly url: string) {
    FakeEventSource.made.push(this)
  }

  addEventListener(name: string, fn: (event: MessageEvent<string>) => void): void {
    this.listeners.set(name, fn)
  }

  close(): void {
    this.closed = true
  }

  emit(name: string, data?: string): void {
    this.listeners.get(name)?.({ data } as MessageEvent<string>)
  }
}

describe('订阅增量', () => {
  /** 建流要先等端口拿到，那是个 Promise。 */
  const settled = () => new Promise((resolve) => setTimeout(resolve, 0))

  beforeEach(() => {
    FakeEventSource.made = []
    vi.stubGlobal('EventSource', FakeEventSource)
  })

  it('没有游标就不带 after_seq，让后端从缓冲里还留着的那段开始', async () => {
    subscribeConversation({ projectCode: PROJECT, conversationId: 'c1', onDelta: () => undefined })
    await settled()
    expect(FakeEventSource.made[0]!.url).toBe(
      'http://127.0.0.1:62066/api/projects/p%20one/conversations/c1/stream',
    )
  })

  it('重连时带上看过的最后一条', async () => {
    subscribeConversation({
      projectCode: PROJECT,
      conversationId: 'c1',
      afterSeq: 7,
      onDelta: () => undefined,
    })
    await settled()
    expect(FakeEventSource.made[0]!.url).toContain('/stream?after_seq=7')
  })

  it('增量原样交给回调，拼接是面板的事', async () => {
    const pieces: string[] = []
    subscribeConversation({
      projectCode: PROJECT,
      conversationId: 'c1',
      onDelta: (piece) => pieces.push(piece),
    })
    await settled()
    FakeEventSource.made[0]!.emit('delta', '赤')
    FakeEventSource.made[0]!.emit('delta', '瞳')
    expect(pieces.join('')).toBe('赤瞳')
  })

  it('一轮出了结果就主动收流——后端收流后浏览器会重连，重连回来只是重推', async () => {
    const turns: number[] = []
    subscribeConversation({
      projectCode: PROJECT,
      conversationId: 'c1',
      onDelta: () => undefined,
      onTurn: (turn) => turns.push(turn.turn_no),
    })
    await settled()
    const source = FakeEventSource.made[0]!
    source.emit('turn', JSON.stringify({ turn_no: 3, drafts: ['d1'] }))
    expect(turns).toEqual([3])
    expect(source.closed).toBe(true)
  })

  it('后端推的失败带 data，浏览器自己的连接错误不带——后者不当成这一轮炸了', async () => {
    const reasons: string[] = []
    subscribeConversation({
      projectCode: PROJECT,
      conversationId: 'c1',
      onDelta: () => undefined,
      onError: (reason) => reasons.push(reason),
    })
    await settled()
    const source = FakeEventSource.made[0]!
    source.emit('error')
    expect(reasons).toEqual([])
    expect(source.closed).toBe(false)

    source.emit('error', '服务商返回了空回答')
    expect(reasons).toEqual(['服务商返回了空回答'])
    expect(source.closed).toBe(true)
  })

  it('退订会关掉流，哪怕它还没建起来', async () => {
    const stop = subscribeConversation({
      projectCode: PROJECT,
      conversationId: 'c1',
      onDelta: () => undefined,
    })
    stop()
    await settled()
    expect(FakeEventSource.made).toHaveLength(0)
  })
})
