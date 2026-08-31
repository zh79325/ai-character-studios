/**
 * 插件管理接口：列插件、触发安装、查进度。
 *
 * 安装是后台线程干的，install 秒回；真进度靠页面轮询 `getPlugin` 拿。
 */
import { request } from './client'

export interface Plugin {
  id: string
  name: string
  description: string
  installed: boolean
  running: boolean
  /** 0-100，安装中才有意义。 */
  progress: number
  /** 预计剩余秒数，安装中才有；刚开始测不到速率时为 null。 */
  eta_seconds: number | null
  /** 已下字节，安装中/刚完成才有意义。 */
  downloaded_bytes: number
  /** 总字节，列文件拿到大小后才 > 0。 */
  total_bytes: number
  /** 当前下载速率（字节/秒），测不到时为 null。 */
  speed_bytes: number | null
  /** 失败时的原因，正常为空串。 */
  message: string
}

export function listPlugins(): Promise<Plugin[]> {
  return request<Plugin[]>('/api/plugins')
}

export function getPlugin(id: string): Promise<Plugin> {
  return request<Plugin>(`/api/plugins/${encodeURIComponent(id)}`)
}

export function installPlugin(id: string): Promise<Plugin> {
  return request<Plugin>(`/api/plugins/${encodeURIComponent(id)}/install`, { method: 'POST' })
}
