import { useParams, type NavigateFunction } from 'react-router-dom'

/** 对项目代号统一做路径段编码，页面和 API 都不得手拼项目路径。 */
export function encodedProjectCode(projectCode: string): string {
  return encodeURIComponent(projectCode)
}

export function projectPath(projectCode: string, suffix = ''): string {
  const base = `/projects/${encodedProjectCode(projectCode)}`
  if (!suffix) return base
  return `${base}/${suffix.replace(/^\/+/, '')}`
}

export function projectApiPath(projectCode: string, suffix = ''): string {
  const base = `/api/projects/${encodedProjectCode(projectCode)}`
  if (!suffix) return base
  return `${base}/${suffix.replace(/^\/+/, '')}`
}

/** draft 收口后替换历史记录，浏览器后退不得回到已经失效的临时代号。 */
export function replaceWithProject(navigate: NavigateFunction, projectCode: string): void {
  navigate(projectPath(projectCode), { replace: true })
}

/** 项目内页面唯一的项目身份来源。 */
export function useProjectCode(): string {
  const { projectCode } = useParams<{ projectCode: string }>()
  if (!projectCode) throw new Error('项目路由缺少 projectCode')
  return projectCode
}
