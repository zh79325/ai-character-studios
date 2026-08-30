/** 选路决策表格。outcome 是这条记录的主角：谁被选中、谁被跳过、为什么。 */
import { Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'

import type { RouteLog } from '@/types/api'

/** 与 provider 路由层写库时用的 outcome 取值对齐（router.py）。 */
const OUTCOME: Record<string, { color: string; label: string }> = {
  selected: { color: 'success', label: '选中' },
  bound: { color: 'success', label: '会话绑定' },
  sticky_hit: { color: 'blue', label: '沿用绑定' },
  rebound: { color: 'geekblue', label: '改绑' },
  rejected: { color: 'error', label: '无可用候选' },
  succeeded: { color: 'success', label: '调用成功' },
  retrying: { color: 'warning', label: '重试' },
  failed: { color: 'error', label: '调用失败' },
}

/** 只显示时分秒：日志面板看的是「刚刚」，年月日是噪音。 */
function clock(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? iso : at.toLocaleTimeString('zh-CN', { hour12: false })
}

export default function RouteLogTable({ logs }: { logs: RouteLog[] }) {
  const columns: ColumnsType<RouteLog> = [
    { title: '时间', dataIndex: 'ts', width: 90, render: clock },
    { title: 'Agent', dataIndex: 'agent_code', width: 150 },
    {
      title: '账号 / 模型',
      width: 260,
      render: (_, row) =>
        row.provider_code === null ? (
          <Typography.Text type="secondary">没落到任何候选</Typography.Text>
        ) : (
          <Space direction="vertical" size={0}>
            <Typography.Text>{row.model_id}</Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {row.provider_code}
            </Typography.Text>
          </Space>
        ),
    },
    {
      title: '结果',
      dataIndex: 'outcome',
      width: 140,
      render: (outcome: string) => {
        const meta = OUTCOME[outcome]
        return <Tag color={meta?.color ?? 'default'}>{meta?.label ?? outcome}</Tag>
      },
    },
    { title: '原因', dataIndex: 'reason', ellipsis: true, render: (v: string | null) => v ?? '—' },
    {
      title: '耗时',
      dataIndex: 'latency_ms',
      width: 90,
      render: (ms: number | null) => (ms === null ? '—' : `${ms} ms`),
    },
    {
      title: '用量',
      width: 130,
      render: (_, row) =>
        row.used_delta === null
          ? '—'
          : `+${row.used_delta.toLocaleString()} ${row.limit_kind ?? ''}`,
    },
    {
      title: '归属',
      width: 120,
      render: (_, row) => (
        <Tooltip title={`会话 ${row.conversation_id ?? '—'}｜任务 ${row.task_id ?? '—'}`}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {row.project_code ?? '—'}
          </Typography.Text>
        </Tooltip>
      ),
    },
  ]

  return (
    <Table<RouteLog>
      rowKey="id"
      size="small"
      pagination={false}
      scroll={{ y: 360 }}
      dataSource={logs}
      columns={columns}
      locale={{ emptyText: '还没有选路记录。跑一次任务就能在这里看到每一步决策。' }}
    />
  )
}
