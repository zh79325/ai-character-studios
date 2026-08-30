/**
 * Agent 配置页：给每个 Agent 指定它能用哪些模型。
 *
 * 三条规矩：**默认一个都不指定**（新装的系统里每个 Agent 都是空的，必须有人来挑，系统
 * 不替你填）；**不把全部模型一股脑塞进去**，候选是显式挑出来的短名单，选项默认只列能力
 * 对得上的模型；**一个 Agent 可以指定多个**，前面的用不了（熔断、额度用尽）才轮到后面的。
 *
 * 绑定关系存在模型那侧（一个模型可以同时被多个 Agent 指定），所以保存时只挑出勾选状态
 * 变了的模型，把它们各自的 Agent 清单整份写回去。
 *
 * 候选顺序不看这里勾选的先后，跟后端选路一致：provider 的 priority 升序，同一个 provider
 * 内按模型 sort_no——想调顺序去 Provider 配置页改优先级。
 *
 * Agent 定义本身是代码资产（`prompts/agents/*.md`），UI 只读。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Checkbox, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState } from 'react'

import { agents as fetchAgents } from '@/api/config'
import { bindAgents, listProviders } from '@/api/providers'
import type { AgentDef, Provider } from '@/types/api'

/** 一个可指定的模型 + 它所属 provider 的排序信息，摊平了好排序、好选。 */
interface Choice {
  key: string
  providerCode: string
  providerName: string
  modelPk: number
  modelId: string
  capabilities: string[]
  /** provider 或模型任一停用，指定了也调不动，界面要说清楚。 */
  usable: boolean
  priority: number
  sortNo: number
  agents: string[]
}

function flatten(providers: Provider[]): Choice[] {
  const rows = providers.flatMap((provider) =>
    provider.models.map((model) => ({
      key: `${provider.code}::${model.id}`,
      providerCode: provider.code,
      providerName: provider.name || provider.code,
      modelPk: model.id,
      modelId: model.model_id,
      capabilities: model.capabilities,
      usable: provider.enabled && model.enabled,
      priority: provider.priority,
      sortNo: model.sort_no,
      agents: model.agents,
    })),
  )
  rows.sort((a, b) => a.priority - b.priority || a.sortNo - b.sortNo)
  return rows
}

export default function AgentsPage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<AgentDef | null>(null)
  const [picked, setPicked] = useState<string[]>([])
  // 能力标注偶尔缺漏，留个后门能看到全部，但默认不给——省得顺手全选
  const [showAll, setShowAll] = useState(false)

  const agentList = useQuery({ queryKey: ['agents'], queryFn: () => fetchAgents() })
  const providers = useQuery({ queryKey: ['providers'], queryFn: listProviders })

  const choices = flatten(providers.data ?? [])
  const assignedTo = (agentCode: string) =>
    choices.filter((item) => item.agents.includes(agentCode))

  const save = useMutation({
    mutationFn: async ({ agentCode, keys }: { agentCode: string; keys: string[] }) => {
      const after = new Set(keys)
      const touched = choices.filter(
        (item) => item.agents.includes(agentCode) !== after.has(item.key),
      )
      // 一个个来而不是并发：同一批请求都在改绑定表，串行最省心，条数也就几个
      for (const item of touched) {
        const next = after.has(item.key)
          ? [...item.agents, agentCode]
          : item.agents.filter((code) => code !== agentCode)
        await bindAgents(item.providerCode, item.modelPk, next)
      }
      return touched.length
    },
    onSuccess: (count) => {
      message.success(count ? `改了 ${count} 个模型的指定` : '没有变化')
      setEditing(null)
      void queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (err: Error) => message.error(err.message),
  })

  const openEditor = (agent: AgentDef) => {
    setEditing(agent)
    setPicked(assignedTo(agent.agent_code).map((item) => item.key))
    setShowAll(false)
  }

  const columns: ColumnsType<AgentDef> = [
    {
      title: 'Agent',
      dataIndex: 'agent_code',
      width: 220,
      render: (code: string, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{code}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {row.role}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '能力',
      dataIndex: 'capability',
      width: 110,
      render: (capability: string) => <Tag color="geekblue">{capability}</Tag>,
    },
    {
      title: '输出',
      dataIndex: 'output_contract',
      width: 120,
      render: (contract: string) => <Tag>{contract}</Tag>,
    },
    {
      title: '指定的模型（按选路顺序）',
      render: (_, row) => {
        const assigned = assignedTo(row.agent_code)
        if (assigned.length === 0) {
          return <Typography.Text type="warning">未指定，这个 Agent 跑不起来</Typography.Text>
        }
        return (
          <Space size={4} wrap>
            {assigned.map((item, index) => (
              <Tag key={item.key} color={item.usable ? 'purple' : 'default'}>
                {index + 1}. {item.providerName} / {item.modelId}
                {!item.usable && '（停用）'}
              </Tag>
            ))}
          </Space>
        )
      },
    },
    {
      title: '',
      width: 100,
      render: (_, row) => (
        <Button size="small" onClick={() => openEditor(row)}>
          指定模型
        </Button>
      ),
    },
  ]

  // 只列能力对得上的；已经指定过的一定要留在选项里，否则回显成一串裸 key
  const matched = editing
    ? choices.filter(
        (item) =>
          showAll ||
          item.capabilities.includes(editing.capability) ||
          item.agents.includes(editing.agent_code),
      )
    : []
  const hiddenCount = choices.length - matched.length

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Typography.Title level={4} style={{ margin: 0 }}>
        Agent 配置
      </Typography.Title>
      <Typography.Text type="secondary">
        Agent 定义随代码走（{'prompts/agents/*.md'}），这里只指定它用哪些模型。默认一个都不指定，
        挑几个够用的就行，别把全部模型都放进去；指定多个时按下面的顺序往下试，
        额度用尽或调用失败才换下一个。
      </Typography.Text>

      <Table<AgentDef>
        rowKey="agent_code"
        size="small"
        loading={agentList.isLoading || providers.isLoading}
        dataSource={agentList.data ?? []}
        columns={columns}
        pagination={false}
        expandable={{
          expandedRowRender: (row) => (
            <Space direction="vertical" size={2}>
              <Typography.Text style={{ fontSize: 12 }}>
                轮次上限 {row.max_turns} · 上下文预算 {row.context_budget.toLocaleString()} · 记忆域{' '}
                {row.memory_scope} · {row.conversational ? '多轮对话' : '单次任务'}
              </Typography.Text>
              <Typography.Text style={{ fontSize: 12 }}>
                工具：{row.allow_tools.length ? row.allow_tools.join('、') : '不给工具'}
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                定义文件 {row.source_file}
              </Typography.Text>
            </Space>
          ),
        }}
        locale={{ emptyText: '没读到 Agent 定义，看后端的 prompts/agents 目录' }}
      />

      <Modal
        open={editing !== null}
        title={editing ? `给 ${editing.agent_code} 指定模型` : ''}
        okText="保存"
        confirmLoading={save.isPending}
        onCancel={() => setEditing(null)}
        onOk={() => editing && save.mutate({ agentCode: editing.agent_code, keys: picked })}
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            清空就是不指定。勾选的先后不算数，实际顺序看 provider 优先级与模型排序号。
          </Typography.Text>
          <Select
            mode="multiple"
            style={{ width: '100%' }}
            value={picked}
            onChange={setPicked}
            placeholder={choices.length ? '挑几个模型' : '还没有模型，先去 Provider 配置页加'}
            disabled={choices.length === 0}
            options={matched.map((item) => ({
              value: item.key,
              label: `${item.providerName} / ${item.modelId}（${item.capabilities.join(
                '、',
              )}）${item.usable ? '' : '［停用］'}`,
            }))}
          />
          {editing && (
            <Checkbox checked={showAll} onChange={(e) => setShowAll(e.target.checked)}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {showAll
                  ? `已列出全部 ${choices.length} 个模型，能力对不上的选了也调不动`
                  : `只列 ${editing.capability} 能力的模型${
                      hiddenCount > 0 ? `，另有 ${hiddenCount} 个被折起来` : ''
                    }`}
              </Typography.Text>
            </Checkbox>
          )}
        </Space>
      </Modal>
    </Space>
  )
}
