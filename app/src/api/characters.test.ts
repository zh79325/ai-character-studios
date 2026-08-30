/**
 * 角色接口的调用形状。
 *
 * 这里钉的是几个容易走形的前后端约定：评审与门禁是两个不同的动作（前端不许拿 `approved` 自动
 * 去 confirm）、驳回后自动重生要带上会话 id、角色 id 进路径要转义。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  advanceCharacter,
  confirmRender,
  confirmSpec,
  confirmViews,
  createCharacter,
  draftAssetSpec,
  generateViews,
  listCharacterEvents,
  listRenders,
  listViews,
  readCharacter,
  rejectRender,
  rejectSpec,
  renderCharacter,
  renderImageUrl,
  reviewSpec,
  reviewViews,
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

describe('渲染图', () => {
  it('只看卡片与真生图是两个端点，不靠一个参数区分', async () => {
    await draftAssetSpec('c1')
    await renderCharacter('c1')

    expect(calls.map((one) => one.url)).toEqual([
      'http://127.0.0.1:62066/api/characters/c1/asset-spec',
      'http://127.0.0.1:62066/api/characters/c1/render',
    ])
    expect(calls.map((one) => one.body)).toEqual([
      { note: '', field: '' },
      { note: '', field: '' },
    ])
  })

  it('改某一项要把那一项单独递过去，不能只给一句话', async () => {
    await renderCharacter('c1', '太暗了', '光照')
    expect(onlyCall().body).toEqual({ note: '太暗了', field: '光照' })
  })

  it('候选列表是只读的', async () => {
    await listRenders('c1')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/characters/c1/renders',
      method: 'GET',
    })
  })

  it('图本体给的是地址，不过 JSON', async () => {
    const url = await renderImageUrl('c1', 'gen-1')

    expect(url).toBe('http://127.0.0.1:62066/api/characters/c1/renders/gen-1/image')
    expect(calls).toHaveLength(0)
  })
})

describe('门禁 2', () => {
  it('采用得指名哪一张，不默认取最新', async () => {
    await confirmRender('c1', 'gen-2', '就这张')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/characters/c1/render/confirm',
      method: 'POST',
      body: { generation_id: 'gen-2', note: '就这张' },
    })
  })

  it('驳回不用指哪一张：停的是这一步，不是某一张', async () => {
    await rejectRender('c1', '尾巴粘在一起了')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/characters/c1/render/reject',
      method: 'POST',
      body: { note: '尾巴粘在一起了' },
    })
  })
})

describe('四视图', () => {
  it('不指定视角就是四个角度都生', async () => {
    await generateViews('c1')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/characters/c1/views',
      method: 'POST',
      body: { variants: [], seed: null },
    })
  })

  it('只重生被点名的那几张，不把已认可的也换掉', async () => {
    await generateViews('c1', ['back'])
    expect(onlyCall().body).toEqual({ variants: ['back'], seed: null })
  })

  it('候选列表是只读的', async () => {
    await listViews('c1')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/characters/c1/views',
      method: 'GET',
    })
  })

  it('评审默认不自动重生：花额度得用户说了算', async () => {
    await reviewViews('c1')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/characters/c1/views/review',
      method: 'POST',
      body: { mode: null, regenerate: false },
    })
  })

  it('粒度可以盖这一次，不用改项目配置', async () => {
    await reviewViews('c1', 'full', true)
    expect(onlyCall().body).toEqual({ mode: 'full', regenerate: true })
  })

  it('定稿四个视角要逐个指名，不默认各取最新', async () => {
    await confirmViews('c1', { front: 'g1', right: 'g2', back: 'g3', left: 'g4' }, '就这一组')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/characters/c1/views/confirm',
      method: 'POST',
      body: {
        picks: { front: 'g1', right: 'g2', back: 'g3', left: 'g4' },
        note: '就这一组',
      },
    })
  })
})
