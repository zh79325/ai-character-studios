import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetBaseUrl } from './client'
import { getPlugin, installPlugin, listPlugins } from './plugins'

beforeEach(() => {
  ;(globalThis as { window?: unknown }).window = {
    atelier: {
      port: () => Promise.resolve(8799),
      startupError: () => Promise.resolve(null),
      logBacklog: () => Promise.resolve([]),
      onBackendLog: () => () => undefined,
    },
  }
})

afterEach(() => {
  vi.restoreAllMocks()
  resetBaseUrl()
  delete (globalThis as { window?: unknown }).window
})

function mockFetch(body: unknown, status = 200) {
  const spy = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status }))
  vi.stubGlobal('fetch', spy)
  return spy
}

const SAMPLE = {
  id: 'whisper-large-v3',
  name: '语音识别模型',
  description: 'x',
  installed: false,
  running: true,
  progress: 42,
  message: '',
}

describe('plugins api', () => {
  it('列插件打到 /api/plugins', async () => {
    const spy = mockFetch([SAMPLE])

    const list = await listPlugins()

    expect(list).toHaveLength(1)
    expect(list[0]!.progress).toBe(42)
    expect(String(spy.mock.calls[0]![0])).toContain('/api/plugins')
  })

  it('安装用 POST 打到 install', async () => {
    const spy = mockFetch(SAMPLE)

    await installPlugin('whisper-large-v3')

    const [url, init] = spy.mock.calls[0]!
    expect(String(url)).toContain('/api/plugins/whisper-large-v3/install')
    expect(init.method).toBe('POST')
  })

  it('查单个插件状态', async () => {
    mockFetch({ ...SAMPLE, installed: true, running: false, progress: 100 })

    const one = await getPlugin('whisper-large-v3')

    expect(one.installed).toBe(true)
    expect(one.progress).toBe(100)
  })
})
