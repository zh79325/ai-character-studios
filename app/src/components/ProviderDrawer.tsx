/**
 * provider 表单。
 *
 * api_key 的语义要写清楚：编辑时留空 = 不动原来的（PATCH 不传该字段），
 * 想清空得点「清空 key」——否则用户没法区分「我没改」和「我要删」。
 */
import { useMutation, useQuery } from '@tanstack/react-query'
import { App, Button, Drawer, Form, Input, InputNumber, Select, Space, Switch } from 'antd'
import { useEffect } from 'react'

import { options as fetchOptions } from '@/api/config'
import { createProvider, patchProvider } from '@/api/providers'
import type { Provider, ProviderIn, ProviderPatch } from '@/types/api'

interface Props {
  open: boolean
  /** null 表示新建。 */
  provider: Provider | null
  onClose: () => void
  onSaved: () => void
}

interface FormValues {
  code: string
  name: string
  base_url: string
  api_key: string
  driver: string
  auth_style: 'bearer' | 'x-api-key'
  priority: number
  enabled: boolean
  verify_ssl: boolean
  remark?: string
}

export default function ProviderDrawer({ open, provider, onClose, onSaved }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<FormValues>()
  const opts = useQuery({ queryKey: ['options'], queryFn: fetchOptions, staleTime: Infinity })
  const isNew = provider === null

  useEffect(() => {
    if (!open) return
    form.setFieldsValue({
      code: provider?.code ?? '',
      name: provider?.name ?? '',
      base_url: provider?.base_url ?? '',
      api_key: '',
      driver: provider?.driver ?? 'openai_compat',
      auth_style: (provider?.auth_style as FormValues['auth_style']) ?? 'bearer',
      priority: provider?.priority ?? 100,
      enabled: provider?.enabled ?? true,
      verify_ssl: provider?.verify_ssl ?? true,
      remark: provider?.remark ?? '',
    })
  }, [open, provider, form])

  const save = useMutation({
    mutationFn: async (values: FormValues) => {
      if (provider === null) {
        const payload: ProviderIn = { ...values, remark: values.remark || null, models: [] }
        return createProvider(payload)
      }
      const { code: _code, api_key, ...rest } = values
      const patch: ProviderPatch = { ...rest, remark: values.remark || null }
      // 留空表示不动原来的 key，非空才带上
      if (api_key) patch.api_key = api_key
      return patchProvider(provider.code, patch)
    },
    onSuccess: () => {
      message.success(isNew ? '账号已建好' : '已保存')
      onSaved()
      onClose()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const clearKey = useMutation({
    mutationFn: () => patchProvider(provider?.code ?? '', { api_key: '' }),
    onSuccess: () => {
      message.success('key 已清空')
      onSaved()
    },
    onError: (err: Error) => message.error(err.message),
  })

  return (
    <Drawer
      open={open}
      width={520}
      title={provider ? `编辑 ${provider.code}` : '新建服务商账号'}
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
          name="code"
          label="账号标识"
          extra="建库后不可改，用它区分同一家的多个账号，如 ark_normal_wytn"
          rules={[
            { required: true, message: '得给个标识' },
            { pattern: /^[A-Za-z0-9_.-]+$/, message: '只能用字母、数字与 _ . -' },
          ]}
        >
          <Input disabled={!isNew} placeholder="ark_coding_plan" />
        </Form.Item>
        <Form.Item name="name" label="显示名">
          <Input placeholder="方舟 Coding Plan（主号）" />
        </Form.Item>
        <Form.Item
          name="base_url"
          label="base_url"
          rules={[{ required: true, message: 'base_url 不能空' }]}
        >
          <Input placeholder="https://ark.cn-beijing.volces.com/api/v3" />
        </Form.Item>
        <Form.Item
          name="api_key"
          label="api_key"
          extra={
            provider === null
              ? '现在不填也行，之后再补'
              : `当前 ${provider.has_key ? provider.api_key_mask : '没配'}；留空表示不改动`
          }
        >
          <Input.Password
            autoComplete="off"
            placeholder={provider === null ? 'sk-...' : '不改就留空'}
          />
        </Form.Item>
        {provider !== null && provider.has_key && (
          <Form.Item>
            <Button
              danger
              size="small"
              loading={clearKey.isPending}
              onClick={() => clearKey.mutate()}
            >
              清空 key
            </Button>
          </Form.Item>
        )}
        <Form.Item name="driver" label="driver" extra="模型没单独指定 driver 时继承这个">
          <Select options={(opts.data?.drivers ?? []).map((v) => ({ value: v, label: v }))} />
        </Form.Item>
        <Form.Item name="auth_style" label="鉴权头">
          <Select
            options={(opts.data?.auth_styles ?? ['bearer', 'x-api-key']).map((v) => ({
              value: v,
              label: v === 'bearer' ? 'Authorization: Bearer（多数）' : 'x-api-key（Meshy 等）',
            }))}
          />
        </Form.Item>
        <Form.Item name="priority" label="优先级" extra="升序，越小越先用">
          <InputNumber min={0} max={9999} style={{ width: '100%' }} />
        </Form.Item>
        <Space size={32}>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="verify_ssl" label="校验证书" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Space>
        <Form.Item name="remark" label="备注">
          <Input.TextArea rows={2} placeholder="到期时间、限额说明之类" />
        </Form.Item>
      </Form>
    </Drawer>
  )
}
