/**
 * 拉起 Python 后端并接住它的端口。
 *
 * 端口不是前端定的，是后端绑完 socket 后从 stdout 第一行告诉我们的（`ATELIER_PORT=xxxxx`）。
 * 这样避免「前端挑个端口结果被占」和「打印出来的端口被别人抢走」两种空窗。
 *
 * 后端也可以自己单独跑（`uv run atelier-serve --port 8799`），这时给 Electron 设
 * `ATELIER_BACKEND_PORT=8799`，它就只连不 spawn——两个进程同时开着会抢同一份 SQLite。
 */
import { spawn, type ChildProcessByStdio } from 'node:child_process'
import { existsSync } from 'node:fs'
import { resolve } from 'node:path'
import type { Readable } from 'node:stream'

export const PORT_LINE_PREFIX = 'ATELIER_PORT='

/** 后端起不来就一直等没意义，超时后放弃并把已收到的输出一起报出去。 */
const START_TIMEOUT_MS = 60_000

/** stdin 用不上（后端不读输入），stdout/stderr 都要读。 */
type BackendProcess = ChildProcessByStdio<null, Readable, Readable>

export interface Backend {
  port: number
  /** 连的是外部已经跑着的后端时为 null，退出时也就不该去杀它。 */
  process: BackendProcess | null
}

/** 认一个端口数字，不像端口就返回 null。 */
function parsePort(raw: string): number | null {
  const port = Number(raw.trim())
  if (!Number.isInteger(port) || port <= 0 || port > 65535) return null
  return port
}

/** 从一行输出里认端口；不是端口行返回 null。 */
export function parsePortLine(line: string): number | null {
  const trimmed = line.trim()
  if (!trimmed.startsWith(PORT_LINE_PREFIX)) return null
  return parsePort(trimmed.slice(PORT_LINE_PREFIX.length))
}

/** 外部后端的端口，没配或配得不像端口就是 null（这时自己 spawn 一个）。 */
export function externalPort(env: NodeJS.ProcessEnv = process.env): number | null {
  const raw = env.ATELIER_BACKEND_PORT
  return raw ? parsePort(raw) : null
}

/** 打包后 server/ 被塞进 resources/，开发时它在仓库根的兄弟目录。 */
export function resolveServerDir(
  appPath: string,
  resourcesPath: string,
  packaged: boolean,
): string {
  if (packaged) return resolve(resourcesPath, 'server')
  const sibling = resolve(appPath, '..', 'server')
  return existsSync(sibling) ? sibling : resolve(appPath, 'server')
}

export interface StartOptions {
  serverDir: string
  /** 每收到一行后端输出就回调一次，主进程转发给渲染层的日志面板。 */
  onLog?: (line: string) => void
}

export function startBackend({ serverDir, onLog }: StartOptions): Promise<Backend> {
  // 用 uv run 而不是直接 python：虚拟环境与依赖同步都交给它，省掉一层环境探测
  const child: BackendProcess = spawn('uv', ['run', 'atelier-serve'], {
    cwd: serverDir,
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  return new Promise<Backend>((fulfil, reject) => {
    const collected: string[] = []
    let settled = false
    let stdoutRest = ''

    const finish = (fn: () => void) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      fn()
    }

    const timer = setTimeout(() => {
      finish(() => {
        child.kill('SIGKILL')
        reject(
          new Error(`后端 ${START_TIMEOUT_MS / 1000}s 内没报出端口：\n${collected.join('\n')}`),
        )
      })
    }, START_TIMEOUT_MS)

    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk: string) => {
      stdoutRest += chunk
      const lines = stdoutRest.split('\n')
      stdoutRest = lines.pop() ?? ''
      for (const line of lines) {
        collected.push(line)
        onLog?.(line)
        const port = parsePortLine(line)
        if (port !== null) finish(() => fulfil({ port, process: child }))
      }
    })

    // uvicorn 的日志走 stderr，那不是错误，一并当日志转发
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk: string) => {
      for (const line of chunk.split('\n')) {
        if (!line) continue
        collected.push(line)
        onLog?.(line)
      }
    })

    child.on('error', (err) => {
      finish(() => reject(new Error(`拉起后端失败（uv 装了吗？）：${err.message}`)))
    })

    child.on('exit', (code) => {
      finish(() => reject(new Error(`后端退出（code=${code}）：\n${collected.join('\n')}`)))
    })
  })
}

/** 先温和地要它收摊，赖着不走再强杀。外部后端不是我们起的，不碰。 */
export function stopBackend(backend: Backend | null, graceMs = 3_000): void {
  if (!backend?.process || backend.process.exitCode !== null) return
  const child = backend.process
  child.kill('SIGTERM')
  const timer = setTimeout(() => {
    if (child.exitCode === null) child.kill('SIGKILL')
  }, graceMs)
  timer.unref?.()
}
