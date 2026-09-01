/**
 * 新建项目的第一步：只选一个目录。
 *
 * 名字、代号、风格都不在这里问——它们是跟 Agent 聊出来的结论，聊之前填的多半要改。这一步
 * 只在目录里放下 `project.json` 与项目库，好让立项对话有地方存。
 *
 * 目录已经归另一个项目时先问一句再覆盖：这一按下去旧项目的配置与会话记录就没了，代价得在
 * 点之前说清，而不是让用户撞一个 409 再自己猜怎么办。
 */
import { useMutation } from '@tanstack/react-query'
import { App, Modal, Space, Typography } from 'antd'
import { useState } from 'react'

import { bootstrapProject, inspectDir } from '@/api/projects'
import DirectoryPicker from '@/components/DirectoryPicker'
import type { ProjectSummary } from '@/types/api'

interface Props {
  open: boolean
  /** 默认项目根，作为目录对话框的起点。 */
  defaultRoot: string
  onClose: () => void
  onCreated: (project: ProjectSummary) => void
}

export default function ProjectBootstrapModal({ open, defaultRoot, onClose, onCreated }: Props) {
  const { message, modal } = App.useApp()
  const [dir, setDir] = useState('')

  const start = useMutation({
    mutationFn: async (path: string) => {
      const state = await inspectDir(path)
      // 用户摇头就当这次没点过：留着目录不清，他改一个路径接着来
      if (state.occupied && !(await askOverwrite(modal, state.marks))) return null
      return bootstrapProject(path, state.occupied)
    },
    onSuccess: (project) => {
      if (project === null) return
      setDir('')
      onCreated(project)
      onClose()
    },
    onError: (err: Error) => message.error(err.message),
  })

  return (
    <Modal
      open={open}
      title="新建项目"
      okText="开始立项对焦"
      confirmLoading={start.isPending}
      okButtonProps={{ disabled: !dir.trim() }}
      onCancel={onClose}
      onOk={() => dir.trim() && start.mutate(dir.trim())}
      destroyOnHidden
    >
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <Typography.Text type="secondary">
          选一个目录存放这个项目的全部产出，里面已经有参考图、旧稿也不要紧。接着在立项页跟设计师聊，
          名字、代号与目录骨架等聊定了再一起落下来。
        </Typography.Text>
        <DirectoryPicker
          value={dir}
          onChange={setDir}
          placeholder={defaultRoot ? `${defaultRoot}/赤瞳系列` : '/Volumes/外置盘/赤瞳系列'}
          defaultPath={defaultRoot}
        />
      </Space>
    </Modal>
  )
}

/**
 * 目录已经归另一个项目：问清楚再覆盖。
 *
 * 只清项目自己的那几样（配置、视觉规范、运行库），用户丢在目录里的素材文件不动——他要的是
 * 「这个目录归新项目」，不是「把我的图删了」。
 */
function askOverwrite(modal: ModalApi, marks: string[]): Promise<boolean> {
  return new Promise((resolve) => {
    modal.confirm({
      title: '这个目录里已经有一个项目',
      content: `覆盖会删掉 ${marks.join('、')} 与 .atelier/ 运行库（旧项目的会话记录一并没了），素材文件不动。`,
      okText: '覆盖',
      okButtonProps: { danger: true },
      cancelText: '换个目录',
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    })
  })
}

type ModalApi = ReturnType<typeof App.useApp>['modal']
