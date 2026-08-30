/** 额度看板的表格本体。窗口用量用进度条表达，红了就是这一窗打满了。 */
import { Button, Popconfirm, Progress, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'

import type { Budget, ModelUsage, UsageBoard } from '@/types/api'

interface Props {
  board: UsageBoard | undefined
  loading: boolean
  onClearBreaker: (code: string, modelPk: number) => void
  onResetUsage: (code: string, modelPk: number, limitKind?: string) => void
}

/** source 说明这个 limit 是谁给的：本地配置说了算，远程只提供 used。 */
const SOURCE_HINT: Record<string, string> = {
  local: '本地统计',
  remote: '远程用量服务',
  mixed: '本地限额 + 远程用量',
}

export default function UsageTable({ board, loading, onClearBreaker, onResetUsage }: Props) {
  const columns: ColumnsType<ModelUsage> = [
    {
      title: '账号 / 模型',
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{row.model_id}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {row.provider_name || row.provider_code}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '可用',
      width: 130,
      render: (_, row) => {
        if (!row.provider_enabled) return <Tag>账号停用</Tag>
        if (!row.enabled) return <Tag>模型停用</Tag>
        if (!row.has_key) return <Tag color="error">缺 key</Tag>
        if (row.breaker) return <Tag color="warning">熔断中</Tag>
        return <Tag color="success">可用</Tag>
      },
    },
    {
      title: 'Agent',
      dataIndex: 'agents',
      width: 200,
      render: (agents: string[]) => (
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
      title: '窗口用量',
      dataIndex: 'budgets',
      render: (budgets: Budget[], row) =>
        budgets.length === 0 ? (
          <Typography.Text type="secondary">没配额度，不限量</Typography.Text>
        ) : (
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            {budgets.map((budget) => (
              <BudgetLine
                key={`${budget.limit_kind}:${budget.window_key}`}
                budget={budget}
                onReset={() =>
                  onResetUsage(row.provider_code, row.provider_model_id, budget.limit_kind)
                }
              />
            ))}
          </Space>
        ),
    },
    {
      title: '',
      width: 120,
      render: (_, row) =>
        row.breaker && (
          <Popconfirm
            title="放行熔断？"
            description={`上次失败：${row.breaker.last_reason ?? '未知'}`}
            onConfirm={() => onClearBreaker(row.provider_code, row.provider_model_id)}
          >
            <Button size="small">放行</Button>
          </Popconfirm>
        ),
    },
  ]

  return (
    <Table<ModelUsage>
      rowKey="provider_model_id"
      size="small"
      loading={loading}
      pagination={false}
      dataSource={board?.items ?? []}
      columns={columns}
      locale={{ emptyText: '还没有配好的模型。先去「服务商设置」加账号与模型，并绑定 Agent。' }}
    />
  )
}

function BudgetLine({ budget, onReset }: { budget: Budget; onReset: () => void }) {
  const percent = budget.unlimited
    ? 0
    : Math.min(100, Math.round((budget.used / budget.limit) * 100))
  return (
    <Space size={8} style={{ width: '100%' }}>
      <Tag style={{ width: 62, textAlign: 'center' }}>{budget.limit_kind}</Tag>
      {budget.unlimited ? (
        <Typography.Text type="secondary">不限量</Typography.Text>
      ) : (
        <Tooltip
          title={`${budget.window_text}｜${SOURCE_HINT[budget.source] ?? budget.source}｜组 ${budget.group_name}`}
        >
          <Progress
            percent={percent}
            size="small"
            status={budget.exhausted ? 'exception' : 'normal'}
            style={{ width: 220, margin: 0 }}
            format={() => `${budget.used.toLocaleString()} / ${budget.limit.toLocaleString()}`}
          />
        </Tooltip>
      )}
      <Typography.Link style={{ fontSize: 12 }} onClick={onReset}>
        清用量
      </Typography.Link>
    </Space>
  )
}
