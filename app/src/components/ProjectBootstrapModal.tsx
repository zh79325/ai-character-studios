/**
 * 新建项目的第一步：只选一个目录。
 *
 * 名字、代号、风格都不在这里问——它们是跟 Agent 聊出来的结论，聊之前填的多半要改。这一步
 * 只在目录里放下 `project.json` 与项目库，好让立项对话有地方存。
 */
import { useMutation } from '@tanstack/react-query'
import { App, Modal, Space, Typography } from 'antd'
import { useState } from 'react'

import { bootstrapProject } from '@/api/projects'
import DirectoryPicker from '@/components/DirectoryPicker'
import type { ProjectList } from '@/types/api'

interface Props {
  open: boolean
  /** 默认项目根，作为目录对话框的起点。 */
  defaultRoot: string
  onClose: () => void
  onCreated: (list: ProjectList) => void
}

export default function ProjectBootstrapModal({ open, defaultRoot, onClose, onCreated }: Props) {
  const { message } = App.useApp()
  const [dir, setDir] = useState('')

  const start = useMutation({
    mutationFn: (path: string) => bootstrapProject(path),
    onSuccess: (list) => {
      setDir('')
      onCreated(list)
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
