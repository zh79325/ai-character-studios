/**
 * 项目接口的调用形状。
 *
 * 这些断言钉住无状态项目协议：项目内请求必须把 project code 放进资源路径，项目管理动作不依赖
 * “当前项目”，配置提交也不能抹掉用户手写的键。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetBaseUrl } from './client'
import {
  bootstrapProject,
  buildConfigPatch,
  finalizeProject,
  forgetProject,
  inspectDir,
  listProjects,
  readConfig,
  scanProject,
  writeArtBible,
} from './projects'
import type { ProjectConfig } from '@/types/api'

interface Call {
  url: string
  method: string
  body: unknown
}

let calls: Call[] = []

beforeEach(() => {
  calls = []
  ;(globalThis as { window?: unknown }).window = {
    atelier: {
      port: () => Promise.resolve(62066),
      startupError: () => Promise.resolve(null),
      logBacklog: () => Promise.resolve([]),
      onBackendLog: () => () => undefined,
      chooseDirectory: () => Promise.resolve(null),
    },
  }
  vi.stubGlobal('fetch', (url: string, init?: { method?: string; body?: string }) => {
    calls.push({
      url,
      method: init?.method ?? 'GET',
      body: init?.body === undefined ? undefined : JSON.parse(init.body),
    })
    return Promise.resolve(new Response('{}', { status: 200 }))
  })
})

afterEach(() => {
  resetBaseUrl()
  vi.unstubAllGlobals()
  delete (globalThis as { window?: unknown }).window
})

/** 断言只发了一个请求，并把它拿出来看。 */
function onlyCall(): Call {
  expect(calls).toHaveLength(1)
  const [call] = calls
  if (!call) throw new Error('一个请求都没发出去')
  return call
}

describe('列表', () => {
  it('默认不扫默认目录——扫盘是用户点了才做的事', async () => {
    await listProjects()
    expect(onlyCall().url).toBe('http://127.0.0.1:62066/api/projects')
  })

  it('要认领手动拷进去的项目才带 sync', async () => {
    await listProjects(true)
    expect(onlyCall().url).toBe('http://127.0.0.1:62066/api/projects?sync=true')
  })
})

describe('立项', () => {
  it('第一步只交目录——名字与代号还没聊出来', async () => {
    await bootstrapProject('/tmp/赤瞳系列')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/projects/bootstrap',
      method: 'POST',
      body: { dir_path: '/tmp/赤瞳系列', overwrite: false },
    })
  })

  it('先问目录现状，界面才知道该不该弹覆盖确认', async () => {
    await inspectDir('/tmp/赤瞳系列')
    expect(onlyCall().url).toBe(
      'http://127.0.0.1:62066/api/projects/dir-state?dir_path=%2Ftmp%2F%E8%B5%A4%E7%9E%B3%E7%B3%BB%E5%88%97',
    )
  })

  it('用户点了覆盖才带 overwrite：它会删旧项目的配置与运行库', async () => {
    await bootstrapProject('/tmp/赤瞳系列', true)
    expect(onlyCall().body).toEqual({ dir_path: '/tmp/赤瞳系列', overwrite: true })
  })

  it('收口作用在 URL 指定的 draft 项目上', async () => {
    await finalizeProject('draft one', { name: '赤瞳', code: 'chitong' })
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/projects/draft%20one/finalize',
      method: 'POST',
      body: { name: '赤瞳', code: 'chitong' },
    })
  })
})

describe('移出项目', () => {
  it('code 进路径要转义，免得带下划线以外的字符时拼歪', async () => {
    await forgetProject('a b')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/projects/a%20b',
      method: 'DELETE',
    })
  })
})

describe('项目内的读写', () => {
  it('读取配置时项目代号位于路径中并经过编码', async () => {
    await readConfig('other project')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/projects/other%20project/config',
      method: 'GET',
    })
  })

  it('art bible 整篇提交到指定项目', async () => {
    await writeArtBible('other', '# 视觉规范\n')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/projects/other/art-bible',
      method: 'PUT',
      body: { content: '# 视觉规范\n' },
    })
  })

  it('扫描是指定项目上的动作，用 POST', async () => {
    await scanProject('other')
    expect(onlyCall()).toMatchObject({
      url: 'http://127.0.0.1:62066/api/projects/other/scan',
      method: 'POST',
    })
  })
})

describe('buildConfigPatch', () => {
  const config = {
    code: 'chitong',
    name: '赤瞳系列',
    style: { art_style: '国风水墨', mood: '', palette: '', quality: '', 我的风格键: '冷色' },
    defaults: {
      image_size: 2048,
      texture_resolution: '2k',
      enable_pbr: true,
      target_polycount: 30000,
      pose_mode: 't-pose',
      height_meters: 1.7,
      我的默认值: 3,
    },
    pose_template: null,
    art_bible: 'art-bible.md',
    review_mode: 'lean',
    conversation_audit: false,
    我的备注: '下周交付',
  } as unknown as ProjectConfig

  const values = {
    name: '改了名',
    review_mode: 'full' as const,
    conversation_audit: true,
    style: { art_style: '蒸汽朋克' },
    defaults: { image_size: 1024 },
  }

  it('后端整段替换 style/defaults，所以用户手写的额外键得原样带回去', () => {
    const patch = buildConfigPatch(config, values)

    expect(patch.style).toEqual({
      art_style: '蒸汽朋克',
      mood: '',
      palette: '',
      quality: '',
      我的风格键: '冷色',
    })
    expect(patch.defaults?.我的默认值).toBe(3)
    expect(patch.defaults?.image_size).toBe(1024)
    expect(patch.defaults?.texture_resolution).toBe('2k')
    expect(patch.conversation_audit).toBe(true)
  })

  it('顶层没画在表单上的键不提交，交给后端的 PATCH 语义留着', () => {
    const patch = buildConfigPatch(config, values)

    expect(Object.keys(patch).sort()).toEqual(
      ['conversation_audit', 'defaults', 'name', 'pose_template', 'review_mode', 'style'].sort(),
    )
  })

  it('姿态模板留空等于不用，统一成 null 而不是空串', () => {
    expect(buildConfigPatch(config, { ...values, pose_template: '   ' }).pose_template).toBeNull()
    expect(
      buildConfigPatch(config, { ...values, pose_template: ' templates/t-pose.png ' })
        .pose_template,
    ).toBe('templates/t-pose.png')
  })
})
