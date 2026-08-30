import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  BackendUnavailable,
  baseUrl,
  errorMessage,
  joinBase,
  resetBaseUrl,
  withQuery,
} from './client'

afterEach(() => {
  resetBaseUrl()
  delete (globalThis as { window?: unknown }).window
})

/** 把 preload 那层伪造出来，省得为了测 URL 拼装去起 Electron。 */
function fakeBridge(port: number | null, startupError: string | null = null) {
  ;(globalThis as { window?: unknown }).window = {
    atelier: {
      port: () => Promise.resolve(port),
      startupError: () => Promise.resolve(startupError),
      logBacklog: () => Promise.resolve([]),
      onBackendLog: () => () => undefined,
    },
  }
}

describe('joinBase', () => {
  it('只连本机', () => {
    expect(joinBase(62066)).toBe('http://127.0.0.1:62066')
  })
})

describe('withQuery', () => {
  it('没参数就不带问号', () => {
    expect(withQuery('/api/providers')).toBe('/api/providers')
  })

  it('空值一律不带，免得后端把空串当有效值', () => {
    expect(withQuery('/api/x', { a: 1, b: null, c: undefined, d: '' })).toBe('/api/x?a=1')
  })

  it('布尔与中文都正确编码', () => {
    expect(withQuery('/api/x', { refresh: true, q: '双尾兽' })).toBe(
      '/api/x?refresh=true&q=%E5%8F%8C%E5%B0%BE%E5%85%BD',
    )
  })
})

describe('baseUrl', () => {
  it('从 preload 拿端口，并且只问一次', async () => {
    fakeBridge(62066)
    const spy = vi.spyOn(
      (globalThis as { window: { atelier: { port: () => Promise<number | null> } } }).window
        .atelier,
      'port',
    )
    expect(await baseUrl()).toBe('http://127.0.0.1:62066')
    expect(await baseUrl()).toBe('http://127.0.0.1:62066')
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('端口拿不到时报出后端给的原因，且不把失败缓存住', async () => {
    fakeBridge(null, 'uv 没装')
    await expect(baseUrl()).rejects.toThrow(BackendUnavailable)
    fakeBridge(5000)
    expect(await baseUrl()).toBe('http://127.0.0.1:5000')
  })
})

describe('errorMessage', () => {
  it('取后端的 detail', async () => {
    const response = new Response(JSON.stringify({ detail: '账号 x 不存在' }), { status: 404 })
    expect(await errorMessage(response)).toBe('账号 x 不存在')
  })

  it('detail 是结构体（422 那种）时原样带出来', async () => {
    const body = { detail: [{ loc: ['body', 'providers'], msg: '类型不对' }] }
    const response = new Response(JSON.stringify(body), { status: 422 })
    expect(await errorMessage(response)).toContain('类型不对')
  })

  it('响应不是 JSON 就退回状态码', async () => {
    const response = new Response('<html>502</html>', { status: 502 })
    expect(await errorMessage(response)).toBe('请求失败（HTTP 502）')
  })
})
