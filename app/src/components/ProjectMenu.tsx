/**
 * 顶栏的项目相关菜单，两个一级项：
 *
 * - 项目快捷入口只在 URL 命中项目路由时出现，名称来自该代号的项目详情；后端不保存当前项目。
 * - 「项目」：进入已有项目、导入项目、新建项目。这些不是项目内路由而是动作，自己消化掉。
 *
 * 点击项目只导航到带代号的 URL，不调用切换接口。
 *
 * 菜单项要拼进顶栏那一整个 Menu，弹窗又得挂在页面上，所以做成 hook 交三样东西给调用方：
 * 菜单项、点击处理、要渲染的弹窗。
 */
import {
  AppstoreOutlined,
  FolderOpenOutlined,
  FolderOutlined,
  PlusOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Modal, Space, Typography } from 'antd'
import type { MenuProps } from 'antd'
import { useState, type ReactNode } from 'react'
import { matchPath, useLocation, useNavigate } from 'react-router-dom'

import { importProject, listProjects, readProject } from '@/api/projects'
import DirectoryPicker from '@/components/DirectoryPicker'
import ProjectBootstrapModal from '@/components/ProjectBootstrapModal'
import { DESIGN_ENTRIES, designPath, projectEntries } from '@/lib/design'
import { projectPath } from '@/lib/projectRoute'
import type { ProjectSummary } from '@/types/api'

/** 菜单 key 都带这个前缀：顶栏其余菜单的 key 是路由路径，前缀一眼分得开。 */
const PREFIX = 'project:'
const OPEN_PREFIX = `${PREFIX}open:`
const IMPORT_KEY = `${PREFIX}import`
const NEW_KEY = `${PREFIX}new`

export interface ProjectMenu {
  /** 拼在顶栏最前面：先是当前项目（没打开就没有这一项），再是项目管理动作。 */
  items: NonNullable<MenuProps['items']>
  /** 命中项目菜单就自己处理掉并返回 true，没命中交回调用方去导航。 */
  handle: (key: string) => boolean
  dialogs: ReactNode
}

export function useProjectMenu(): ProjectMenu {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [creating, setCreating] = useState(false)
  const [importDir, setImportDir] = useState<string | null>(null)
  const { pathname } = useLocation()
  const projectCode = matchPath('/projects/:projectCode/*', pathname)?.params.projectCode

  const list = useQuery({ queryKey: ['projects'], queryFn: () => listProjects() })
  const detail = useQuery({
    queryKey: ['project', projectCode],
    queryFn: () => readProject(projectCode!),
    enabled: Boolean(projectCode),
    retry: false,
  })
  const projects = list.data?.projects ?? []
  const current = detail.data ?? projects.find((one) => one.code === projectCode) ?? null

  const enter = (project: ProjectSummary) => {
    queryClient.setQueryData(['project', project.code], project)
    void queryClient.invalidateQueries({ queryKey: ['projects'] })
    navigate(projectPath(project.code))
  }

  const doImport = useMutation({
    mutationFn: (dir: string) => importProject(dir),
    onSuccess: (project) => {
      message.success(`已导入 ${project.name}`)
      setImportDir(null)
      enter(project)
    },
    onError: (err: Error) => message.error(err.message),
  })

  const openList: MenuProps['items'] = projects.length
    ? projects.map((project) => ({
        key: `${OPEN_PREFIX}${project.code}`,
        // 目录不在（外置盘没挂、被搬走）就别让用户点进去，进去每个页面都会报错
        disabled: project.missing,
        label: project.missing ? `${project.name}（目录不在）` : project.name,
      }))
    : [{ key: `${PREFIX}empty`, disabled: true, label: '还没有项目' }]

  const manage = {
    key: 'project',
    icon: <FolderOutlined />,
    label: '项目',
    children: [
      { key: `${PREFIX}open`, label: '进入已有项目', children: openList },
      { key: IMPORT_KEY, icon: <FolderOpenOutlined />, label: '导入项目' },
      { key: NEW_KEY, icon: <PlusOutlined />, label: '新建项目' },
    ],
  }

  // 立项没收口时素材目录还没铺，那些入口点进去只有一句「先完成立项」，不如先按下不表
  const drafting = current?.stage === 'drafting'
  const currentItem = current && {
    key: 'current-project',
    icon: <AppstoreOutlined />,
    label: current.name,
    children: [
      ...projectEntries(current.code).map((entry) => ({ key: entry.key, label: entry.label })),
      { type: 'divider' as const },
      ...DESIGN_ENTRIES.map((entry) => ({
        key: designPath(current.code, entry.slug),
        disabled: drafting,
        label: entry.ready ? entry.label : `${entry.label}（即将开放）`,
      })),
    ],
  }

  const handle = (key: string) => {
    if (key.startsWith(OPEN_PREFIX)) {
      navigate(projectPath(key.slice(OPEN_PREFIX.length)))
      return true
    }
    if (key === IMPORT_KEY) {
      setImportDir('')
      return true
    }
    if (key === NEW_KEY) {
      setCreating(true)
      return true
    }
    // 剩下的自家 key（如占位项）也吞掉，别拿它当路由去导航
    return key.startsWith(PREFIX)
  }

  const dialogs = (
    <>
      <ProjectBootstrapModal
        open={creating}
        defaultRoot={list.data?.default_root ?? ''}
        onClose={() => setCreating(false)}
        onCreated={enter}
      />
      <Modal
        open={importDir !== null}
        title="导入项目"
        okText="导入并进入"
        confirmLoading={doImport.isPending}
        okButtonProps={{ disabled: !importDir?.trim() }}
        onCancel={() => setImportDir(null)}
        onOk={() => importDir?.trim() && doImport.mutate(importDir.trim())}
        destroyOnHidden
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Text type="secondary">
            选中的目录里得有 `project.json`；代号与配置都以那份文件为准。
          </Typography.Text>
          <DirectoryPicker
            value={importDir ?? ''}
            onChange={setImportDir}
            placeholder="/Volumes/外置盘/赤瞳系列"
            defaultPath={list.data?.default_root}
          />
        </Space>
      </Modal>
    </>
  )

  return { items: currentItem ? [currentItem, manage] : [manage], handle, dialogs }
}
