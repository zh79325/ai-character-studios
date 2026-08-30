/**
 * 项目接口。
 *
 * 两条约定值得在这里写清楚，因为它们决定了页面怎么写：
 * 一是换项目是 `PUT /current` 这一个明确动作，读接口上的 `?project=` 只是「看一眼别的
 * 项目」，不会把用户的当前项目换掉；二是列表类接口一律返回整份 ProjectList，前端拿到
 * 响应直接换掉缓存即可，不必自己算「切完之后谁是当前项目」。
 */
import { request, withQuery } from './client'
import type {
  ArtBible,
  Character,
  ProjectConfig,
  ProjectConfigPatch,
  ProjectFinalizeIn,
  ProjectList,
  ProjectSummary,
  ScanResult,
} from '@/types/api'

/** `sync` 会先扫一遍默认项目根，把用户手动拷进去的目录认领进来。 */
export function listProjects(sync = false): Promise<ProjectList> {
  return request<ProjectList>(withQuery('/api/projects', { sync: sync || null }))
}

/** 立项第一步：占下目录并切过去，接下来在立项页跟 Agent 对焦。 */
export function bootstrapProject(dirPath: string): Promise<ProjectList> {
  return request<ProjectList>('/api/projects/bootstrap', {
    method: 'POST',
    body: { dir_path: dirPath },
  })
}

/** 立项收口：定下名字与代号，后端顺手铺素材目录、git 规则与 art bible。 */
export function finalizeProject(payload: ProjectFinalizeIn): Promise<ProjectList> {
  return request<ProjectList>('/api/projects/current/finalize', { method: 'POST', body: payload })
}

/** 挂上一个已有的项目目录：换机器、外置盘、同事拷来的都走这里。 */
export function importProject(dirPath: string): Promise<ProjectList> {
  return request<ProjectList>('/api/projects/import', {
    method: 'POST',
    body: { dir_path: dirPath },
  })
}

export function switchProject(code: string): Promise<ProjectList> {
  return request<ProjectList>('/api/projects/current', { method: 'PUT', body: { code } })
}

/** 从本机移出，磁盘上的文件一个不动——项目目录是用户的资产。 */
export function forgetProject(code: string): Promise<ProjectList> {
  return request<ProjectList>(`/api/projects/${encodeURIComponent(code)}`, { method: 'DELETE' })
}

/** 没选过项目时后端是 404，调用方据此引导用户先新建或导入。 */
export function currentProject(): Promise<ProjectSummary> {
  return request<ProjectSummary>('/api/projects/current')
}

export function readConfig(project?: string): Promise<ProjectConfig> {
  return request<ProjectConfig>(withQuery('/api/projects/current/config', { project }))
}

export function updateConfig(patch: ProjectConfigPatch): Promise<ProjectConfig> {
  return request<ProjectConfig>('/api/projects/current/config', { method: 'PUT', body: patch })
}

export function readArtBible(project?: string): Promise<ArtBible> {
  return request<ArtBible>(withQuery('/api/projects/current/art-bible', { project }))
}

/** 整篇覆盖保存：art bible 是视觉真相，编辑器给的就是全文。 */
export function writeArtBible(content: string): Promise<ArtBible> {
  return request<ArtBible>('/api/projects/current/art-bible', { method: 'PUT', body: { content } })
}

export function scanProject(): Promise<ScanResult> {
  return request<ScanResult>('/api/projects/current/scan', { method: 'POST' })
}

export function listCharacters(project?: string): Promise<Character[]> {
  return request<Character[]>(withQuery('/api/projects/current/characters', { project }))
}

/**
 * 用表单值拼配置提交体。
 *
 * 后端收到 `style` / `defaults` 是整段替换，而表单只认识自己画出来的那几个字段——直接提交
 * 会把用户手写在 `project.json` 里的额外键抹掉。所以这里以读回来的配置为底再盖表单值：
 * 平台不认识的键原样带回去。
 */
export function buildConfigPatch(
  config: ProjectConfig,
  values: {
    name: string
    review_mode: ProjectConfig['review_mode']
    pose_template?: string
    style: Partial<ProjectConfig['style']>
    defaults: Partial<ProjectConfig['defaults']>
  },
): ProjectConfigPatch {
  return {
    name: values.name,
    review_mode: values.review_mode,
    // 空串表示「不用姿态模板」，后端的 null 与它同义，统一成 null 免得存一个空字符串进 json
    pose_template: values.pose_template?.trim() ? values.pose_template.trim() : null,
    style: { ...config.style, ...values.style },
    defaults: { ...config.defaults, ...values.defaults },
  }
}
