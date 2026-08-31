/**
 * provider 表单。
 *
 * 新建时先选一个套餐预设：端点、driver、鉴权头与模型清单都是供应商定死的事实，逐项手填只是
 * 抄一遍还容易抄错。用户只剩三样东西要填——key、优先级、每个模型的额度数字。预设只给初值，
 * 保存走的仍是 `POST /api/providers`，所以预设不对当场就能改；「自定义」则完全手填。
 *
 * api_key 的语义要写清楚：编辑时留空 = 不动原来的（PATCH 不传该字段），
 * 想清空得点「清空 key」——否则用户没法区分「我没改」和「我要删」。
 */
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  Alert,
  App,
  Button,
  Checkbox,
  Drawer,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { useEffect, useState } from 'react'

import { agents as fetchAgents, options as fetchOptions } from '@/api/config'
import { createProvider, listPresets, patchProvider } from '@/api/providers'
import { type ModelRow, pickedModels, rowsFromPreset } from '@/lib/presets'
import type { Provider, ProviderIn, ProviderPatch } from '@/types/api'

/** 预设下拉里的「自己填」那一项。 */
const CUSTOM = '__custom__'

const UNITS: Record<string, string> = { tokens: 'token', calls: '次', credits: '积分' }

interface Props {
  open: boolean
  /** null 表示新建。 */
  provider: Provider | null
  onClose: () => void
  onSaved: () => void
}

/** 表单里的一份账号。`preset` 只影响初值，不会提交给后端。 */
interface FormValues {
  preset: string
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
  models: ModelRow[]
}

export default function ProviderDrawer({ open, provider, onClose, onSaved }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<FormValues>()
  const opts = useQuery({ queryKey: ['options'], queryFn: fetchOptions, staleTime: Infinity })
  const isNew = provider === null
  const presets = useQuery({
    queryKey: ['provider-presets'],
    queryFn: listPresets,
    staleTime: Infinity,
    enabled: open && isNew,
  })
  const agentList = useQuery({
    queryKey: ['agents'],
    queryFn: () => fetchAgents(),
    staleTime: Infinity,
    enabled: open && isNew,
  })
  const [picked, setPicked] = useState(CUSTOM)

  useEffect(() => {
    if (!open) return
    setPicked(CUSTOM)
    form.setFieldsValue({
      preset: CUSTOM,
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
      models: [],
    })
  }, [open, provider, form])

  const chosen = presets.data?.find((one) => one.code === picked) ?? null

  /** 选中一个套餐：把既定事实填进表单，额度数字与 key 留给用户。 */
  const applyPreset = (code: string) => {
    setPicked(code)
    if (code === CUSTOM) {
      form.setFieldsValue({ models: [] })
      return
    }
    const preset = presets.data?.find((one) => one.code === code)
    if (!preset) return

    form.setFieldsValue({
      code: preset.code,
      name: preset.label,
      base_url: preset.base_url,
      driver: preset.driver,
      auth_style: preset.auth_style as FormValues['auth_style'],
    })
    // 单独一步：`setFieldsValue` 的递归 Partial 认不了 params 里的 unknown 值
    // 不绑 Agent 的模型永远轮不到，所以按能力先绑上，用户再减
    form.setFieldValue('models', rowsFromPreset(preset, agentList.data ?? []))
  }

  const save = useMutation({
    mutationFn: async (values: FormValues) => {
      if (provider === null) {
        const payload: ProviderIn = {
          code: values.code,
          name: values.name,
          base_url: values.base_url,
          api_key: values.api_key,
          enabled: values.enabled,
          priority: values.priority,
          driver: values.driver,
          auth_style: values.auth_style,
          verify_ssl: values.verify_ssl,
          remark: values.remark || null,
          models: pickedModels(values.models ?? []),
        }
        return createProvider(payload)
      }
      const { code: _code, api_key, preset: _preset, models: _models, ...rest } = values
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

  const periodOptions = Object.entries(opts.data?.period_examples ?? {}).map(([expr, hint]) => ({
    value: expr,
    label: `${expr} — ${hint}`,
  }))

  /** 额度输入框后面那个单位：没它的话「1800000」到底是 token 还是次数得猜。 */
  const unitOf = (kind: string) => UNITS[kind] ?? kind

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
        {isNew && (
          <Form.Item
            name="preset"
            label="用哪个套餐"
            extra="选一个就把端点、driver 与模型清单填齐，你只要补 key、优先级与额度"
          >
            <Select
              loading={presets.isLoading}
              onChange={applyPreset}
              options={[
                ...(presets.data ?? []).map((one) => ({
                  value: one.code,
                  label: `${one.label}（${one.models.length} 个模型）`,
                })),
                { value: CUSTOM, label: '自定义（端点与模型自己填）' },
              ]}
            />
          </Form.Item>
        )}
        {chosen?.key_prefix && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={`这个套餐的 key 以 ${chosen.key_prefix} 开头`}
            description="填错了往往不报错，只是不走套餐额度而另行计费。"
          />
        )}
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

        {isNew && chosen !== null && <ModelRows unit={unitOf} periodOptions={periodOptions} />}
      </Form>
    </Drawer>
  )
}

/**
 * 预设带出的模型清单：一行一个，只要填额度数字。
 *
 * 模型名、driver、api_path 与计量口径都不给改：那是供应商定的，改了只会报 404。真要改
 * 得建完在模型抽屉里改，那里每一项都能改。
 */
function ModelRows({
  unit,
  periodOptions,
}: {
  unit: (kind: string) => string
  periodOptions: { value: string; label: string }[]
}) {
  return (
    <>
      <Typography.Text strong>模型与额度</Typography.Text>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
        额度留空就是不限量——不限量只是不拦，真用完了供应商那边依旧报错。不想用的模型把前面的勾
        取消，不会建进去。
      </Typography.Paragraph>
      <Form.List name="models">
        {(fields) => (
          <>
            {fields.map((field) => (
              <ModelRowItem
                key={field.key}
                name={field.name}
                unit={unit}
                periodOptions={periodOptions}
              />
            ))}
          </>
        )}
      </Form.List>
    </>
  )
}

function ModelRowItem({
  name,
  unit,
  periodOptions,
}: {
  name: number
  unit: (kind: string) => string
  periodOptions: { value: string; label: string }[]
}) {
  const form = Form.useFormInstance<FormValues>()
  const row = Form.useWatch(['models', name], form) as ModelRow | undefined
  if (!row) return null

  return (
    <Space align="baseline" style={{ display: 'flex', marginBottom: 8 }}>
      <Form.Item name={[name, 'picked']} valuePropName="checked" style={{ marginBottom: 0 }}>
        <Checkbox />
      </Form.Item>
      <Space direction="vertical" size={0} style={{ width: 210 }}>
        <Tooltip title={row.remark ?? ''}>
          <Typography.Text style={{ fontSize: 13 }}>{row.model_id}</Typography.Text>
        </Tooltip>
        <Space size={4}>
          {row.capabilities.map((one) => (
            <Tag key={one} style={{ fontSize: 11, lineHeight: '16px', marginInlineEnd: 2 }}>
              {one}
            </Tag>
          ))}
        </Space>
      </Space>
      <Form.Item name={[name, 'max_value']} style={{ marginBottom: 0, width: 150 }}>
        <InputNumber
          min={0}
          style={{ width: '100%' }}
          addonAfter={unit(row.limit_kind)}
          placeholder="不限量"
        />
      </Form.Item>
      <Form.Item name={[name, 'period_expr']} style={{ marginBottom: 0, width: 190 }}>
        <Select showSearch options={periodOptions} />
      </Form.Item>
    </Space>
  )
}
