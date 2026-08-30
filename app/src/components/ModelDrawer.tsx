/**
 * 模型表单。一个 provider 下可以挂多个模型，每个模型自己带能力、额度与 Agent 绑定。
 *
 * params 用裸 JSON 输入：Meshy 的单价表 `credit_costs` 这类东西形态不固定，
 * 硬做成表单只会限制它；JSON 解析失败就当校验不通过，不让用户存进去一段废话。
 */
import { MinusCircleOutlined, PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  App,
  Button,
  Drawer,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Tooltip,
  Typography,
} from 'antd'
import { useEffect } from 'react'

import { agents as fetchAgents, options as fetchOptions } from '@/api/config'
import { saveModel, updateModel } from '@/api/providers'
import { kindLabel } from '@/lib/limits'
import type { Model, ModelIn, Provider } from '@/types/api'

const CAPABILITIES = ['text', 'vision', 'image', 'video', 'mesh']

interface Props {
  open: boolean
  provider: Provider
  /** null 表示新加一个模型。 */
  model: Model | null
  onClose: () => void
  onSaved: () => void
}

interface LimitRow {
  limit_kind: string
  max_value: number
  period_expr: string
  group_name: string
}

interface FormValues {
  model_id: string
  capabilities: string[]
  driver?: string | null
  api_path?: string
  enabled: boolean
  sort_no: number
  agents: string[]
  limits: LimitRow[]
  params: string
  remark?: string
}

export default function ModelDrawer({ open, provider, model, onClose, onSaved }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<FormValues>()
  const opts = useQuery({ queryKey: ['options'], queryFn: fetchOptions, staleTime: Infinity })
  const agentList = useQuery({
    queryKey: ['agents'],
    queryFn: () => fetchAgents(),
    staleTime: Infinity,
  })

  useEffect(() => {
    if (!open) return
    form.setFieldsValue({
      model_id: model?.model_id ?? '',
      capabilities: model?.capabilities ?? ['text'],
      driver: model?.driver ?? null,
      api_path: model?.api_path ?? '',
      enabled: model?.enabled ?? true,
      sort_no: model?.sort_no ?? 0,
      agents: model?.agents ?? [],
      limits: (model?.limits ?? []).map((limit) => ({
        limit_kind: limit.limit_kind,
        max_value: limit.max_value,
        period_expr: limit.period_expr,
        group_name: limit.group_name,
      })),
      params: JSON.stringify(model?.params ?? {}, null, 2),
      remark: model?.remark ?? '',
    })
  }, [open, model, form])

  const save = useMutation({
    mutationFn: (values: FormValues) => {
      const payload: ModelIn = {
        model_id: values.model_id,
        capabilities: values.capabilities,
        driver: values.driver || null,
        api_path: values.api_path || null,
        enabled: values.enabled,
        sort_no: values.sort_no,
        params: JSON.parse(values.params || '{}') as Record<string, unknown>,
        remark: values.remark || null,
        agents: values.agents,
        limits: values.limits ?? [],
      }
      // 已有模型走 PUT：它认 provider_model_id，改 model_id 也不会变成新增一条
      return model === null
        ? saveModel(provider.code, payload)
        : updateModel(provider.code, model.id, payload)
    },
    onSuccess: () => {
      message.success('已保存')
      onSaved()
      onClose()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const periodOptions = Object.entries(opts.data?.period_examples ?? {}).map(([expr, hint]) => ({
    value: expr,
    label: `${expr} — ${hint}`,
  }))

  return (
    <Drawer
      open={open}
      width={620}
      title={model === null ? `给 ${provider.code} 加模型` : `编辑 ${model.model_id}`}
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
            保存
          </Button>
        </Space>
      }
    >
      <Form form={form} layout="vertical" requiredMark="optional">
        <Form.Item
          name="model_id"
          label="model_id"
          extra="就是请求体里那个 model 字段，如 doubao-seed-1-6-251015"
          rules={[{ required: true, message: '得填 model_id' }]}
        >
          <Input placeholder="doubao-seed-1-6-251015" />
        </Form.Item>
        <Form.Item name="capabilities" label="能力" rules={[{ required: true }]}>
          <Select mode="multiple" options={CAPABILITIES.map((v) => ({ value: v, label: v }))} />
        </Form.Item>
        <Form.Item name="driver" label="driver" extra={`留空继承 provider 的 ${provider.driver}`}>
          <Select
            allowClear
            placeholder={`继承 ${provider.driver}`}
            options={(opts.data?.drivers ?? []).map((v) => ({ value: v, label: v }))}
          />
        </Form.Item>
        <Form.Item name="api_path" label="api_path" extra="留空用 driver 的默认路径">
          <Input placeholder="/chat/completions" />
        </Form.Item>
        <Form.Item
          name="agents"
          label="绑定 Agent"
          extra="不绑就永远轮不到它。Agent 定义来自 prompts/agents/*.md，这里只能选不能改"
        >
          <Select
            mode="multiple"
            loading={agentList.isLoading}
            options={(agentList.data ?? []).map((agent) => ({
              value: agent.agent_code,
              label: `${agent.agent_code}（${agent.capability}）`,
            }))}
          />
        </Form.Item>

        <Typography.Text strong>额度</Typography.Text>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
          每种口径最多一条。同一 group_name 的模型共享一个窗口计数（同号多模型共用限额就靠它）。
        </Typography.Paragraph>
        <Form.List name="limits">
          {(fields, { add, remove }) => (
            <>
              {fields.map((field) => (
                <Space
                  key={field.key}
                  align="baseline"
                  style={{ display: 'flex', marginBottom: 8 }}
                >
                  <Form.Item
                    name={[field.name, 'limit_kind']}
                    rules={[{ required: true, message: '选口径' }]}
                    style={{ marginBottom: 0, width: 170 }}
                  >
                    <Select
                      placeholder="口径"
                      options={(opts.data?.limit_kinds ?? []).map((v) => ({
                        value: v,
                        label: kindLabel(v),
                      }))}
                    />
                  </Form.Item>
                  <Form.Item
                    name={[field.name, 'max_value']}
                    rules={[{ required: true, message: '填上限' }]}
                    style={{ marginBottom: 0, width: 150 }}
                  >
                    <InputNumber min={0} style={{ width: '100%' }} placeholder="上限，0=不限" />
                  </Form.Item>
                  <Form.Item
                    name={[field.name, 'period_expr']}
                    rules={[{ required: true, message: '填周期' }]}
                    style={{ marginBottom: 0, width: 170 }}
                  >
                    <Select showSearch placeholder="day / day+11H" options={periodOptions} />
                  </Form.Item>
                  <Form.Item
                    name={[field.name, 'group_name']}
                    style={{ marginBottom: 0, width: 110 }}
                  >
                    <Input placeholder="default" />
                  </Form.Item>
                  <Tooltip title="删掉这条额度">
                    <MinusCircleOutlined onClick={() => remove(field.name)} />
                  </Tooltip>
                </Space>
              ))}
              <Form.Item>
                <Button
                  block
                  icon={<PlusOutlined />}
                  onClick={() =>
                    add({ limit_kind: 'tokens', period_expr: 'day', group_name: 'default' })
                  }
                >
                  加一条额度
                </Button>
              </Form.Item>
            </>
          )}
        </Form.List>

        <Form.Item
          name="params"
          label="params（JSON）"
          extra='调用参数。Meshy 的每次操作单价写在这里：{"credit_costs": {"image_to_3d": 5}}'
          rules={[
            {
              validator: (_rule, value: string) => {
                if (!value?.trim()) return Promise.resolve()
                try {
                  const parsed: unknown = JSON.parse(value)
                  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
                    return Promise.reject(new Error('得是一个 JSON 对象'))
                  }
                  return Promise.resolve()
                } catch {
                  return Promise.reject(new Error('这不是合法 JSON'))
                }
              },
            },
          ]}
        >
          <Input.TextArea rows={4} style={{ fontFamily: 'Menlo, monospace' }} />
        </Form.Item>

        <Space size={32}>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="sort_no" label="同号内顺序" tooltip="升序，越小越先用">
            <InputNumber min={0} />
          </Form.Item>
        </Space>
        <Form.Item name="remark" label="备注">
          <Input.TextArea rows={2} />
        </Form.Item>
      </Form>
    </Drawer>
  )
}
