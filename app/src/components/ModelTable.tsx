/** provider 展开行里的模型清单：能力、生效 driver、绑定的 Agent、额度窗口一眼看全。 */
import { DeleteOutlined, EditOutlined } from '@ant-design/icons'
import { useMutation } from '@tanstack/react-query'
import { App, Button, Popconfirm, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'

import { deleteModel } from '@/api/providers'
import type { Model, Provider } from '@/types/api'

interface Props {
  provider: Provider
  onEdit: (model: Model) => void
  onChanged: () => void
}

export default function ModelTable({ provider, onEdit, onChanged }: Props) {
  const { message } = App.useApp()

  const remove = useMutation({
    mutationFn: (modelPk: number) => deleteModel(provider.code, modelPk),
    onSuccess: () => {
      message.success('模型已删')
      onChanged()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const columns: ColumnsType<Model> = [
    {
      title: '模型',
      dataIndex: 'model_id',
      render: (id: string, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{id}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {row.endpoint}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '能力',
      dataIndex: 'capabilities',
      width: 160,
      render: (caps: string[]) => (
        <Space size={4} wrap>
          {caps.map((cap) => (
            <Tag key={cap}>{cap}</Tag>
          ))}
        </Space>
      ),
    },
    {
      title: 'driver',
      dataIndex: 'effective_driver',
      width: 150,
      render: (driver: string, row) => (
        <Space size={4}>
          <Tag color="blue">{driver}</Tag>
          {row.driver === null && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              继承
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: '绑定 Agent',
      dataIndex: 'agents',
      render: (agents: string[]) =>
        agents.length === 0 ? (
          <Typography.Text type="secondary">没绑，永远不会被选到</Typography.Text>
        ) : (
          <Space size={4} wrap>
            {agents.map((code) => (
              <Tag key={code} color="purple">
                {code}
              </Tag>
            ))}
          </Space>
        ),
    },
    {
      title: '额度',
      dataIndex: 'limits',
      width: 240,
      render: (limits: Model['limits']) =>
        limits.length === 0 ? (
          <Typography.Text type="secondary">不限</Typography.Text>
        ) : (
          <Space direction="vertical" size={0}>
            {limits.map((limit) => (
              <Typography.Text key={limit.id} style={{ fontSize: 12 }}>
                {limit.limit_kind} {limit.max_value.toLocaleString()} / {limit.window_text}
                {limit.group_name !== 'default' && `（组 ${limit.group_name}）`}
              </Typography.Text>
            ))}
          </Space>
        ),
    },
    {
      title: '状态',
      dataIndex: 'enabled',
      width: 80,
      render: (enabled: boolean) => (enabled ? <Tag color="success">启用</Tag> : <Tag>停用</Tag>),
    },
    {
      title: '',
      width: 100,
      render: (_, row) => (
        <Space size={4}>
          <Button size="small" icon={<EditOutlined />} onClick={() => onEdit(row)} />
          <Popconfirm
            title={`删除 ${row.model_id}？`}
            description="它的额度、用量与绑定一起删。"
            okButtonProps={{ danger: true }}
            onConfirm={() => remove.mutate(row.id)}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Table<Model>
      rowKey="id"
      size="small"
      pagination={false}
      dataSource={provider.models}
      columns={columns}
    />
  )
}
