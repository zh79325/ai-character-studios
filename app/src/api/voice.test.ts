import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, resetBaseUrl } from './client'
import { transcribe } from './voice'

beforeEach(() => {
  // baseUrl 要读 window.atelier.port()，测试环境没有 window，先伪造一个。
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

/** 没有 Electron bridge 时 baseUrl 退到默认端口，够测请求形状了。 */
function mockFetch(response: Response) {
  const spy = vi.fn().mockResolvedValue(response)
  vi.stubGlobal('fetch', spy)
  return spy
}

describe('transcribe', () => {
  it('把录音以 FormData 发到 /api/transcribe 并取回文本', async () => {
    const fetchSpy = mockFetch(new Response(JSON.stringify({ text: '你好世界' }), { status: 200 }))

    const text = await transcribe(new Blob(['x'], { type: 'audio/webm' }))

    expect(text).toBe('你好世界')
    const [url, init] = fetchSpy.mock.calls[0]!
    expect(String(url)).toContain('/api/transcribe')
    expect(init.method).toBe('POST')
    expect(init.body).toBeInstanceOf(FormData)
    expect((init.body as FormData).get('audio')).toBeInstanceOf(Blob)
  })

  it('后端报错就抛 ApiError 并带上 detail', async () => {
    mockFetch(new Response(JSON.stringify({ detail: '语音模型还没装' }), { status: 503 }))

    await expect(transcribe(new Blob())).rejects.toMatchObject({
      name: 'ApiError',
      status: 503,
      message: '语音模型还没装',
    })
    await expect(transcribe(new Blob())).rejects.toBeInstanceOf(ApiError)
  })
})
