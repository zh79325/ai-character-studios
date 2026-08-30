/**
 * 渲染层唯一能碰到 Node 的门。
 *
 * 只暴露四件事：后端端口、启动错误、日志回放与订阅。别的一律走 HTTP，主进程不当代理。
 */
import { contextBridge, ipcRenderer } from 'electron'

import type { AtelierBridge } from '../src/types/bridge'

const api: AtelierBridge = {
  port: () => ipcRenderer.invoke('atelier:port') as Promise<number | null>,
  startupError: () => ipcRenderer.invoke('atelier:startup-error') as Promise<string | null>,
  logBacklog: () => ipcRenderer.invoke('atelier:log-backlog') as Promise<string[]>,
  onBackendLog: (handler) => {
    const listener = (_event: unknown, line: string) => handler(line)
    ipcRenderer.on('atelier:backend-log', listener)
    return () => ipcRenderer.off('atelier:backend-log', listener)
  },
}

contextBridge.exposeInMainWorld('atelier', api)
