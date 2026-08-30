/**
 * 服务商设置页：一个 provider 一行，展开是它下面的模型。
 *
 * 明文 key 只在表单里出现一次（用户自己敲的那次），列表里永远是掩码。
 */
import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Popconfirm, Space, Switch, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState } from 'react'

import { deleteProvider, listProviders, patchProvider } from '@/api/providers'
import ModelDrawer from '@/components/ModelDrawer'
import ModelTable from '@/components/ModelTable'
import PortableCard from '@/components/PortableCard'
import ProviderDrawer from '@/components/ProviderDrawer'
import type { Model, Provider } from '@/types/api'

export default function ProvidersPage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const providers = useQuery({ queryKey: ['providers'], queryFn: listProviders })

  const [editing, setEditing] = useState<Provider | null>(null)
  const [creating, setCreating] = useState(false)
  const [modelTarget, setModelTarget] = useState<{
    provider: Provider
    model: Model | null
  } | null>(null)

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['providers'] })
    void queryClient.invalidateQueries({ queryKey: ['usage'] })
  }

  const toggle = useMutation({
    mutationFn: (row: Provider) => patchProvider(row.code, { enabled: !row.enabled }),
    onSuccess: invalidate,
    onError: (err: Error) => message.error(err.message),
  })

  const remove = useMutation({
    mutationFn: (code: string) => deleteProvider(code),
    onSuccess: () => {
      message.success('已删除')
      invalidate()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const columns: ColumnsType<Provider> = [
    {
      title: '账号',
      dataIndex: 'code',
      render: (code: string, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{row.name || code}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {code}
          </Typography.Text>
        </Space>
      ),
    },
    { title: 'base_url', dataIndex: 'base_url', ellipsis: true },
    { title: 'driver', dataIndex: 'driver', width: 140, render: (v: string) => <Tag>{v}</Tag> },
    {
      title: 'api_key',
      dataIndex: 'api_key_mask',
      width: 160,
      render: (mask: string, row) =>
        row.has_key ? (
          <Typography.Text code>{mask}</Typography.Text>
        ) : (
          <Tag color="error">没配</Tag>
        ),
    },
    { title: '优先级', dataIndex: 'priority', width: 90 },
    {
      title: '启用',
      dataIndex: 'enabled',
      width: 80,
      render: (enabled: boolean, row) => (
        <Switch
          size="small"
          checked={enabled}
          loading={toggle.isPending && toggle.variables?.code === row.code}
          onChange={() => toggle.mutate(row)}
        />
      ),
    },
    {
      title: '操作',
      width: 220,
      render: (_, row) => (
        <Space size={4}>
          <Button size="small" icon={<EditOutlined />} onClick={() => setEditing(row)}>
            编辑
          </Button>
          <Button
            size="small"
            icon={<PlusOutlined />}
            onClick={() => setModelTarget({ provider: row, model: null })}
          >
            加模型
          </Button>
          <Popconfirm
            title={`删除 ${row.code}？`}
            description="连它的模型、额度、用量与 Agent 绑定一起删。"
            okButtonProps={{ danger: true }}
            onConfirm={() => remove.mutate(row.code)}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Space style={{ justifyContent: 'space-between', width: '100%' }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          服务商设置
        </Typography.Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>
          新建账号
        </Button>
      </Space>

      <Table<Provider>
        rowKey="code"
        size="small"
        loading={providers.isLoading}
        dataSource={providers.data ?? []}
        columns={columns}
        pagination={false}
        expandable={{
          expandedRowRender: (row) => (
            <ModelTable
              provider={row}
              onEdit={(model) => setModelTarget({ provider: row, model })}
              onChanged={invalidate}
            />
          ),
          rowExpandable: (row) => row.models.length > 0,
        }}
        locale={{ emptyText: '还没有账号。先「新建账号」，或者在下面导入一份配置。' }}
      />

      <PortableCard onImported={invalidate} />

      <ProviderDrawer
        open={creating || editing !== null}
        provider={editing}
        onClose={() => {
          setCreating(false)
          setEditing(null)
        }}
        onSaved={invalidate}
      />

      {modelTarget && (
        <ModelDrawer
          open
          provider={modelTarget.provider}
          model={modelTarget.model}
          onClose={() => setModelTarget(null)}
          onSaved={invalidate}
        />
      )}
    </Space>
  )
}
