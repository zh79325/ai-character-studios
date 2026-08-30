/**
 * 顶栏的「项目」菜单：打开已有项目、导入项目、新建项目。
 *
 * 打开哪个项目，之后干活就在哪个项目里，所以这里没有「切换」这个动作，也不标谁是当前。
 *
 * 菜单项要拼进顶栏那一整个 Menu，弹窗又得挂在页面上，所以做成 hook 交三样东西给调用方：
 * 菜单项、点击处理、要渲染的弹窗。
 */
import { FolderOpenOutlined, FolderOutlined, PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Modal, Space, Typography } from 'antd'
import type { MenuProps } from 'antd'
import { useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'

import { importProject, listProjects, switchProject } from '@/api/projects'
import DirectoryPicker from '@/components/DirectoryPicker'
import ProjectCreateDrawer from '@/components/ProjectCreateDrawer'
import type { ProjectList } from '@/types/api'

/** 菜单 key 都带这个前缀：顶栏其余菜单的 key 是路由路径，前缀一眼分得开。 */
const PREFIX = 'project:'
const OPEN_PREFIX = `${PREFIX}open:`
const IMPORT_KEY = `${PREFIX}import`
const NEW_KEY = `${PREFIX}new`

export interface ProjectMenu {
  item: NonNullable<MenuProps['items']>[number]
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

  const list = useQuery({ queryKey: ['projects'], queryFn: () => listProjects() })
  const projects = list.data?.projects ?? []

  /**
   * 打开项目等于换一个库，缓存里所有项目相关的东西一律作废。
   *
   * 逐个点名 key 迟早会漏（后面还要加会话、素材、渲染图），而这个动作是用户主动做的、
   * 一秒钟一次都不到，全刷一遍的代价可以忽略。
   */
  const adopt = (fresh: ProjectList) => {
    queryClient.setQueryData(['projects'], fresh)
    void queryClient.invalidateQueries()
  }

  const open = useMutation({
    mutationFn: (code: string) => switchProject(code),
    onSuccess: (fresh) => {
      adopt(fresh)
      navigate('/project')
    },
    onError: (err: Error) => message.error(err.message),
  })

  const doImport = useMutation({
    mutationFn: (dir: string) => importProject(dir),
    onSuccess: (fresh) => {
      message.success(`已打开 ${fresh.current}`)
      setImportDir(null)
      adopt(fresh)
      navigate('/project')
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

  const item = {
    key: 'project',
    icon: <FolderOutlined />,
    label: '项目',
    children: [
      { key: `${PREFIX}open`, label: '打开已有项目', children: openList },
      { key: IMPORT_KEY, icon: <FolderOpenOutlined />, label: '导入项目' },
      { key: NEW_KEY, icon: <PlusOutlined />, label: '新建项目' },
    ],
  }

  const handle = (key: string) => {
    if (key.startsWith(OPEN_PREFIX)) {
      open.mutate(key.slice(OPEN_PREFIX.length))
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
      <ProjectCreateDrawer
        open={creating}
        defaultRoot={list.data?.default_root ?? ''}
        onClose={() => setCreating(false)}
        onCreated={(fresh) => {
          adopt(fresh)
          navigate('/project')
        }}
      />
      <Modal
        open={importDir !== null}
        title="导入项目"
        okText="导入并打开"
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

  return { item, handle, dialogs }
}
