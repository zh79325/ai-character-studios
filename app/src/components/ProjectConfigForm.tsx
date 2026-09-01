/**
 * 项目配置表单。
 *
 * 真相在项目目录的 `project.json` 里，这个表单只是它的一个视图：每次打开都重新读盘，
 * 用户在编辑器里手改过也能看到。保存是 PATCH 语义，没画在表单上的键（含用户自己加的）
 * 由 `buildConfigPatch` 原样带回去。
 *
 * 三张卡片共用一个 `<Form>`：一个 useForm 实例只能接一个 Form 元素，拆成三个会让前两张
 * 卡片的字段收不上来。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Card, Col, Form, Input, InputNumber, Row, Select, Space, Switch } from 'antd'
import { useEffect } from 'react'

import { buildConfigPatch, readConfig, updateConfig } from '@/api/projects'
import { useProjectCode } from '@/lib/projectRoute'
import type { ProjectConfig, ReviewMode } from '@/types/api'

export const REVIEW_MODES = [
  { value: 'full', label: 'full：每一步都过评审' },
  { value: 'lean', label: 'lean：关键节点过评审（推荐）' },
  { value: 'solo', label: 'solo：只在门禁处过评审' },
]

interface FormValues {
  name: string
  review_mode: ReviewMode
  conversation_audit: boolean
  pose_template?: string
  art_style: string
  mood: string
  palette: string
  quality: string
  image_size: number
  texture_resolution: string
  enable_pbr: boolean
  target_polycount: number
  pose_mode: string
  height_meters: number
}

function toForm(config: ProjectConfig): FormValues {
  return {
    name: config.name,
    review_mode: config.review_mode,
    conversation_audit: config.conversation_audit,
    pose_template: config.pose_template ?? '',
    art_style: config.style.art_style,
    mood: config.style.mood,
    palette: config.style.palette,
    quality: config.style.quality,
    image_size: config.defaults.image_size,
    texture_resolution: config.defaults.texture_resolution,
    enable_pbr: config.defaults.enable_pbr,
    target_polycount: config.defaults.target_polycount,
    pose_mode: config.defaults.pose_mode,
    height_meters: config.defaults.height_meters,
  }
}

export default function ProjectConfigForm() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const projectCode = useProjectCode()
  const [form] = Form.useForm<FormValues>()
  const config = useQuery({
    queryKey: ['project', projectCode, 'config'],
    queryFn: () => readConfig(projectCode),
  })

  useEffect(() => {
    if (config.data) form.setFieldsValue(toForm(config.data))
  }, [config.data, form])

  const save = useMutation({
    mutationFn: (values: FormValues) => {
      if (!config.data) throw new Error('配置还没读上来')
      return updateConfig(
        projectCode,
        buildConfigPatch(config.data, {
          name: values.name,
          review_mode: values.review_mode,
          conversation_audit: values.conversation_audit,
          pose_template: values.pose_template,
          style: {
            art_style: values.art_style,
            mood: values.mood,
            palette: values.palette,
            quality: values.quality,
          },
          defaults: {
            image_size: values.image_size,
            texture_resolution: values.texture_resolution,
            enable_pbr: values.enable_pbr,
            target_polycount: values.target_polycount,
            pose_mode: values.pose_mode,
            height_meters: values.height_meters,
          },
        }),
      )
    },
    onSuccess: (fresh) => {
      message.success('已写回 project.json')
      queryClient.setQueryData(['project', projectCode, 'config'], fresh)
      // 显示名跟着改了，项目列表与当前项目摘要得刷新
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
      void queryClient.invalidateQueries({ queryKey: ['project', projectCode] })
    },
    onError: (err: Error) => message.error(err.message),
  })

  return (
    <Form
      form={form}
      layout="vertical"
      requiredMark="optional"
      onFinish={(values) => save.mutate(values)}
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Card
          size="small"
          title="项目信息"
          loading={config.isLoading}
          extra={
            <Space>
              <Button onClick={() => void config.refetch()}>从磁盘重载</Button>
              <Button type="primary" htmlType="submit" loading={save.isPending}>
                保存
              </Button>
            </Space>
          }
        >
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="name"
                label="项目名"
                extra="只改显示名，不搬目录——目录一改，已存的相对路径就全失效了"
                rules={[{ required: true, message: '名字不能空' }]}
              >
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item label="项目代号" extra="跟着目录走的身份，建完不改">
                <Input value={config.data?.code ?? ''} disabled />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="review_mode" label="评审强度">
                <Select options={REVIEW_MODES} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="conversation_audit"
                label="对话审计"
                valuePropName="checked"
                extra="开启后，LLM 的 Request/Response 写入目标目录的 tmp/conversation/"
              >
                <Switch checkedChildren="开启" unCheckedChildren="关闭" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="pose_template"
                label="姿态模板"
                extra="四视图用的参考底图，相对项目目录；留空表示不用"
              >
                <Input placeholder="templates/t-pose.png" />
              </Form.Item>
            </Col>
          </Row>
        </Card>

        <Card size="small" title="风格" loading={config.isLoading}>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="art_style" label="美术风格">
                <Input placeholder="国风水墨" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="mood" label="氛围">
                <Input placeholder="冷峻、克制" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="palette" label="配色">
                <Input placeholder="赤红为主，佐以墨黑" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="quality" label="质感要求">
                <Input placeholder="厚涂笔触，避免塑料光泽" />
              </Form.Item>
            </Col>
          </Row>
        </Card>

        <Card
          size="small"
          title="出图与建模默认值"
          loading={config.isLoading}
          extra="每次生成没单独指定时用这些"
        >
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="image_size" label="图像边长（px）">
                <InputNumber min={256} max={8192} step={256} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="texture_resolution" label="贴图分辨率">
                <Select options={['1k', '2k', '4k'].map((value) => ({ value, label: value }))} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="target_polycount" label="目标面数">
                <InputNumber min={1000} step={1000} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="pose_mode" label="姿态">
                <Select options={['t-pose', 'a-pose'].map((value) => ({ value, label: value }))} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="height_meters" label="身高（米）" extra="给建模定尺度">
                <InputNumber min={0.1} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="enable_pbr" label="PBR 贴图" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
          </Row>
        </Card>
      </Space>
    </Form>
  )
}
