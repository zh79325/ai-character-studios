/**
 * 接上 Python 后端：能追到现成的就用现成的，追不到才自己拉一个。
 *
 * 两个后端同时开着会抢同一份 SQLite，还会把会话发到不是你看日志那个进程上，所以启动顺序是：
 *
 * 1. `ATELIER_BACKEND_PORT` 配了就只连不 spawn（后端自己 `--reload` 调时用）；
 * 2. 对固定端口探一下 `/api/health`，活的就用它；
 * 3. 探不通才 spawn。
 *
 * 自己 spawn 时仍等后端从 stdout 第一行报出端口（`ATELIER_PORT=xxxxx`），那是「已经在监听」的信号，
 * 比前端猜一个启动耗时靠谱。
 */
import { spawn, type ChildProcessByStdio } from 'node:child_process'
import { existsSync } from 'node:fs'
import { resolve } from 'node:path'
import type { Readable } from 'node:stream'

export const PORT_LINE_PREFIX = 'ATELIER_PORT='

/** 后端的固定端口，跟 settings.py 里 `port` 的默认值对齐。 */
export const DEFAULT_PORT = 8799

/** 后端起不来就一直等没意义，超时后放弃并把已收到的输出一起报出去。 */
const START_TIMEOUT_MS = 60_000

/** 探活只给本机一下：这个端口上要么已经有个能应答的后端，要么就没人。 */
const PROBE_TIMEOUT_MS = 1_500

/** 后端报出端口到真的开始监听中间隔着 uvicorn 启动，这个间隔轮着探。 */
const READY_POLL_MS = 200

const delay = (ms: number) => new Promise<void>((done) => setTimeout(done, ms))

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

/** 探一下这个端口上真的是个能服务的后端。 */
export async function probeBackend(port: number, timeoutMs = PROBE_TIMEOUT_MS): Promise<boolean> {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/api/health`, {
      signal: AbortSignal.timeout(timeoutMs),
    })
    return response.ok
  } catch {
    return false
  }
}

export interface StartOptions {
  serverDir: string
  /** 每收到一行后端输出就回调一次，主进程转发给渲染层的日志面板。 */
  onLog?: (line: string) => void
}

/**
 * 拿到一个能用的后端：先看环境变量，再探固定端口，都不成才 spawn。
 *
 * 环境变量那条不探活：用户既然指死了端口，就该连它并在前端看见「后端没起来」，而不是默默地另拉一个。
 */
export async function ensureBackend({ serverDir, onLog }: StartOptions): Promise<Backend> {
  const configured = externalPort()
  if (configured !== null) {
    onLog?.(`[electron] ATELIER_BACKEND_PORT=${configured}，只连不 spawn`)
    return { port: configured, process: null }
  }

  if (await probeBackend(DEFAULT_PORT)) {
    onLog?.(`[electron] 后端已经在 127.0.0.1:${DEFAULT_PORT} 上跑着，直接用它`)
    return { port: DEFAULT_PORT, process: null }
  }

  return startBackend({ serverDir, onLog })
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

    // 后端是先报端口再交给 uvicorn，拿到端口只说明它要起了；探通了才能交给上层发请求
    const waitReady = async (port: number) => {
      while (!settled) {
        if (await probeBackend(port)) {
          finish(() => fulfil({ port, process: child }))
          return
        }
        await delay(READY_POLL_MS)
      }
    }

    const timer = setTimeout(() => {
      finish(() => {
        child.kill('SIGKILL')
        reject(new Error(`后端 ${START_TIMEOUT_MS / 1000}s 内没起来：\n${collected.join('\n')}`))
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
        if (port !== null && !settled) void waitReady(port)
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
