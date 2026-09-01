/**
 * 项目接口。
 *
 * 项目管理接口返回注册表或单个项目摘要；项目内接口都以 `projectCode` 为首个必需参数，
 * 直接映射到 URL 中的项目资源，不读取任何隐式“当前项目”状态。
 */
import { request, withQuery } from './client'
import { projectApiPath } from '@/lib/projectRoute'
import type {
  ArtBible,
  Character,
  ProjectConfig,
  ProjectConfigPatch,
  ProjectDirState,
  ProjectFinalizeIn,
  ProjectList,
  ProjectSummary,
  ScanResult,
} from '@/types/api'

/** `sync` 会先扫一遍默认项目根，把用户手动拷进去的目录认领进来。 */
export function listProjects(sync = false): Promise<ProjectList> {
  return request<ProjectList>(withQuery('/api/projects', { sync: sync || null }))
}

/** 选完目录先问一句：已经归另一个项目的话，得先让用户点头再覆盖。 */
export function inspectDir(dirPath: string): Promise<ProjectDirState> {
  return request<ProjectDirState>(withQuery('/api/projects/dir-state', { dir_path: dirPath }))
}

/**
 * 立项第一步：占下目录并切过去，接下来在立项页跟 Agent 对焦。
 *
 * `overwrite` 只在用户对着确认框点过头之后带：它会抹掉目录里旧项目的 `project.json`、
 * `art-bible.md` 与 `.atelier/` 运行库（素材文件不动）。
 */
export function bootstrapProject(dirPath: string, overwrite = false): Promise<ProjectSummary> {
  return request<ProjectSummary>('/api/projects/bootstrap', {
    method: 'POST',
    body: { dir_path: dirPath, overwrite },
  })
}

/** 立项收口：定下名字与代号，后端顺手铺素材目录、git 规则与 art bible。 */
export function finalizeProject(
  projectCode: string,
  payload: ProjectFinalizeIn,
): Promise<ProjectSummary> {
  return request<ProjectSummary>(projectApiPath(projectCode, 'finalize'), {
    method: 'POST',
    body: payload,
  })
}

/** 挂上一个已有的项目目录：换机器、外置盘、同事拷来的都走这里。 */
export function importProject(dirPath: string): Promise<ProjectSummary> {
  return request<ProjectSummary>('/api/projects/import', {
    method: 'POST',
    body: { dir_path: dirPath },
  })
}

/** 从本机移出，磁盘上的文件一个不动——项目目录是用户的资产。 */
export function forgetProject(code: string): Promise<void> {
  return request<void>(projectApiPath(code), { method: 'DELETE' })
}

/** 按 URL 中的项目代号读取摘要。 */
export function readProject(projectCode: string): Promise<ProjectSummary> {
  return request<ProjectSummary>(projectApiPath(projectCode))
}

export function readConfig(projectCode: string): Promise<ProjectConfig> {
  return request<ProjectConfig>(projectApiPath(projectCode, 'config'))
}

export function updateConfig(
  projectCode: string,
  patch: ProjectConfigPatch,
): Promise<ProjectConfig> {
  return request<ProjectConfig>(projectApiPath(projectCode, 'config'), {
    method: 'PUT',
    body: patch,
  })
}

export function readArtBible(projectCode: string): Promise<ArtBible> {
  return request<ArtBible>(projectApiPath(projectCode, 'art-bible'))
}

/** 整篇覆盖保存：art bible 是视觉真相，编辑器给的就是全文。 */
export function writeArtBible(projectCode: string, content: string): Promise<ArtBible> {
  return request<ArtBible>(projectApiPath(projectCode, 'art-bible'), {
    method: 'PUT',
    body: { content },
  })
}

export function scanProject(projectCode: string): Promise<ScanResult> {
  return request<ScanResult>(projectApiPath(projectCode, 'scan'), { method: 'POST' })
}

export function listCharacters(projectCode: string): Promise<Character[]> {
  return request<Character[]>(projectApiPath(projectCode, 'characters'))
}

/** 指定项目 `characters/` 下的分组目录（含空分组）。分组只是文件夹，后端直接读盘。 */
export function listGroups(projectCode: string): Promise<string[]> {
  return request<string[]>(projectApiPath(projectCode, 'groups'))
}

/** 建一个空分组文件夹，返回建完后的最新分组列表。 */
export function createGroup(projectCode: string, path: string): Promise<string[]> {
  return request<string[]>(projectApiPath(projectCode, 'groups'), {
    method: 'POST',
    body: { path },
  })
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
    conversation_audit: boolean
    pose_template?: string
    style: Partial<ProjectConfig['style']>
    defaults: Partial<ProjectConfig['defaults']>
  },
): ProjectConfigPatch {
  return {
    name: values.name,
    review_mode: values.review_mode,
    conversation_audit: values.conversation_audit,
    // 空串表示「不用姿态模板」，后端的 null 与它同义，统一成 null 免得存一个空字符串进 json
    pose_template: values.pose_template?.trim() ? values.pose_template.trim() : null,
    style: { ...config.style, ...values.style },
    defaults: { ...config.defaults, ...values.defaults },
  }
}
