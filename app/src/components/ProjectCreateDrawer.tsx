/**
 * 新建项目。
 *
 * 目录留空就建在默认项目根下，填了就建到磁盘上任意位置——项目自带 `project.json` 与
 * `.atelier/` 运行库，整份拷到别的机器还是同一个项目，所以放在哪儿是用户的事。
 *
 * 风格几项在这里就问，是因为它们会进每一次生图的提示词；建完再补也行，但先有一版比
 * 一直空着好。
 */
import { useMutation } from '@tanstack/react-query'
import { App, Button, Drawer, Form, Input, Select, Space, Typography } from 'antd'
import { useEffect } from 'react'

import { createProject } from '@/api/projects'
import DirectoryPicker from '@/components/DirectoryPicker'
import type { ProjectList, ReviewMode } from '@/types/api'

interface Props {
  open: boolean
  /** 默认项目根，作为目录输入框的提示与对话框起点。 */
  defaultRoot: string
  onClose: () => void
  onCreated: (list: ProjectList) => void
}

interface FormValues {
  name: string
  code: string
  dir_path?: string
  art_style?: string
  mood?: string
  palette?: string
  quality?: string
  review_mode: ReviewMode
}

export const REVIEW_MODES = [
  { value: 'full', label: 'full：每一步都过评审' },
  { value: 'lean', label: 'lean：关键节点过评审（推荐）' },
  { value: 'solo', label: 'solo：只在门禁处过评审' },
]

export default function ProjectCreateDrawer({ open, defaultRoot, onClose, onCreated }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<FormValues>()

  useEffect(() => {
    if (!open) return
    form.setFieldsValue({ review_mode: 'lean' })
  }, [open, form])

  const save = useMutation({
    mutationFn: (values: FormValues) =>
      createProject({
        name: values.name.trim(),
        code: values.code.trim(),
        dir_path: values.dir_path?.trim() || null,
        style: {
          art_style: values.art_style ?? '',
          mood: values.mood ?? '',
          palette: values.palette ?? '',
          quality: values.quality ?? '',
        },
        review_mode: values.review_mode,
      }),
    onSuccess: (list) => {
      message.success('项目已建好，并且已经切过去了')
      form.resetFields()
      onCreated(list)
      onClose()
    },
    onError: (err: Error) => message.error(err.message),
  })

  return (
    <Drawer
      open={open}
      width={520}
      title="新建项目"
      onClose={onClose}
      destroyOnHidden
      extra={
        <Space>
          <Button onClick={onClose}>取消</Button>
          <Button
            type="primary"
            loading={save.isPending}
            onClick={() => void form.validateFields().then((values) => save.mutate(values))}
          >
            建立
          </Button>
        </Space>
      }
    >
      <Form form={form} layout="vertical" requiredMark="optional">
        <Form.Item name="name" label="项目名" rules={[{ required: true, message: '得给个名字' }]}>
          <Input placeholder="赤瞳系列" />
        </Form.Item>
        <Form.Item
          name="code"
          label="项目代号"
          extra="会进目录名以外的所有地方：提示词、日志、外部接口参数，所以只收英文与数字"
          rules={[
            { required: true, message: '得给个代号' },
            { pattern: /^[A-Za-z0-9_-]+$/, message: '只能用英文字母、数字、- 和 _' },
          ]}
        >
          <Input placeholder="chitong" />
        </Form.Item>
        <Form.Item
          name="dir_path"
          label="项目目录"
          extra={
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              留空就建在 {defaultRoot} 下面；也可以指到外置盘或任意工作目录
            </Typography.Text>
          }
        >
          <DirectoryPicker placeholder="留空用默认位置" defaultPath={defaultRoot} />
        </Form.Item>
        <Form.Item name="art_style" label="美术风格">
          <Input placeholder="国风水墨 / 蒸汽朋克 / 半写实二次元" />
        </Form.Item>
        <Form.Item name="mood" label="氛围">
          <Input placeholder="冷峻、克制" />
        </Form.Item>
        <Form.Item name="palette" label="配色">
          <Input placeholder="赤红为主，佐以墨黑" />
        </Form.Item>
        <Form.Item name="quality" label="质感要求">
          <Input placeholder="厚涂笔触，避免塑料光泽" />
        </Form.Item>
        <Form.Item name="review_mode" label="评审强度" extra="之后能在项目配置里改">
          <Select options={REVIEW_MODES} />
        </Form.Item>
      </Form>
    </Drawer>
  )
}
