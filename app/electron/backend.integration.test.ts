/**
 * 真的把后端拉起来一次，验证「spawn → 读首行端口 → 能应答 /api/health → 收摊」这条链子。
 *
 * 它依赖本机装了 uv 且 server/ 依赖同步过，所以默认不跑；
 * 要跑就 `ATELIER_E2E=1 npm test`。
 */
import { resolve } from 'node:path'

import { afterAll, describe, expect, it } from 'vitest'

import { resolveServerDir, startBackend, stopBackend, type Backend } from './backend'

const enabled = process.env.ATELIER_E2E === '1'
let backend: Backend | null = null

afterAll(() => stopBackend(backend))

describe.skipIf(!enabled)('startBackend', () => {
  it('拿到端口并且这个端口真的在服务', async () => {
    const serverDir = resolveServerDir(resolve(import.meta.dirname, '..'), '', false)
    backend = await startBackend({ serverDir })

    expect(backend.port).toBeGreaterThan(0)
    const response = await fetch(`http://127.0.0.1:${backend.port}/api/health`)
    expect(response.ok).toBe(true)
    const body = (await response.json()) as { ok: boolean; runtime_db: string }
    expect(body.ok).toBe(true)
    expect(body.runtime_db).toContain('runtime.db')
  }, 90_000)
})
