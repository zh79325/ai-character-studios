import { describe, expect, it, vi } from 'vitest'

import { designPath } from '@/lib/design'
import { projectApiPath, projectPath, replaceWithProject } from '@/lib/projectRoute'

describe('项目路由', () => {
  it('生成项目首页、配置、领域和角色路径', () => {
    expect(projectPath('demo')).toBe('/projects/demo')
    expect(projectPath('demo', 'config')).toBe('/projects/demo/config')
    expect(designPath('demo', 'characters')).toBe('/projects/demo/design/characters')
    expect(projectPath('demo', 'characters/CHAR-1')).toBe('/projects/demo/characters/CHAR-1')
  })

  it('项目代号作为单个路径段编码', () => {
    expect(projectPath('a/b c', '/art-bible')).toBe('/projects/a%2Fb%20c/art-bible')
    expect(projectApiPath('a/b c', '/characters')).toBe('/api/projects/a%2Fb%20c/characters')
  })

  it('finalize 后替换到正式项目地址', () => {
    const navigate = vi.fn()

    replaceWithProject(navigate, 'ready project')

    expect(navigate).toHaveBeenCalledWith('/projects/ready%20project', { replace: true })
  })
})
