/**
 * Agent 配置页：给每个 Agent 指定它能用哪些模型。
 *
 * 三条规矩：**默认一个都不指定**（新装的系统里每个 Agent 都是空的，必须有人来挑，系统
 * 不替你填）；**不把全部模型一股脑塞进去**，候选是显式挑出来的短名单，选项默认只列能力
 * 对得上的模型；**一个 Agent 可以指定多个**，前面的用不了（熔断、额度用尽）才轮到后面的。
 *
 * 绑定关系存在模型那侧（一个模型可以同时被多个 Agent 指定），所以保存时只挑出指定状态
 * 变了的模型，把它们各自的 Agent 清单整份写回去。
 *
 * 候选顺序不看添加的先后，跟后端选路一致：provider 的 priority 升序，同一个 provider
 * 内按模型 sort_no——想调顺序去 Provider 配置页改优先级。
 *
 * 限额也在这里配：只有真被指定的模型才需要限额，弹窗里点「限额」就能改（存的是模型级额度，
 * 跟 Agent 绑定分两次保存）。
 *
 * Agent 定义本身是代码资产（`prompts/agents/*.md`），UI 只读。
 */
import { MinusCircleOutlined, PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  App,
  Button,
  Input,
  InputNumber,
  List,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState } from 'react'

import { agents as fetchAgents, options as fetchOptions } from '@/api/config'
import { bindAgents, listProviders, updateModel } from '@/api/providers'
import { kindLabel, limitText } from '@/lib/limits'
import type { AgentDef, LimitIn, Model, Provider } from '@/types/api'

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
  /** 改限额要整份提交模型，原样留着好重建 payload。 */
  model: Model
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
      model,
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
  // 添加区的两步：先 provider，再模型。模型只在点「添加」时才进已指定列表
  const [providerCode, setProviderCode] = useState<string | null>(null)
  const [candidate, setCandidate] = useState<string | null>(null)
  // 能力标注偶尔缺漏，留个后门能看到全部，但默认不给
  const [showAll, setShowAll] = useState(false)
  // 限额就在这里顺手设：只有真被指定的模型才需要限额，没必要去 Provider 页给每个模型都配
  const [limitTarget, setLimitTarget] = useState<Choice | null>(null)
  const [limitRows, setLimitRows] = useState<LimitIn[]>([])

  const agentList = useQuery({ queryKey: ['agents'], queryFn: () => fetchAgents() })
  const providers = useQuery({ queryKey: ['providers'], queryFn: listProviders })
  const opts = useQuery({ queryKey: ['options'], queryFn: fetchOptions, staleTime: Infinity })

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
    setProviderCode(null)
    setCandidate(null)
    setShowAll(false)
  }

  /** 改限额要整份提交模型，除了 limits 其他字段原样送回去。 */
  const saveLimits = useMutation({
    mutationFn: ({ target, limits }: { target: Choice; limits: LimitIn[] }) => {
      const model = target.model
      return updateModel(target.providerCode, target.modelPk, {
        model_id: model.model_id,
        capabilities: model.capabilities,
        driver: model.driver,
        api_path: model.api_path,
        enabled: model.enabled,
        sort_no: model.sort_no,
        params: model.params,
        remark: model.remark,
        // 绑定跟限额分两次保存，这里只能送后端当下的 agents，不能拿未保存的指定覆盖它
        agents: model.agents,
        limits,
      })
    },
    onSuccess: () => {
      message.success('限额已保存')
      setLimitTarget(null)
      void queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (err: Error) => message.error(err.message),
  })

  const openLimits = (choice: Choice) => {
    setLimitTarget(choice)
    setLimitRows(
      choice.model.limits.map((limit) => ({
        limit_kind: limit.limit_kind,
        max_value: limit.max_value,
        group_name: limit.group_name,
        period_expr: limit.period_expr,
      })),
    )
  }

  const patchLimit = (index: number, patch: Partial<LimitIn>) =>
    setLimitRows(limitRows.map((row, i) => (i === index ? { ...row, ...patch } : row)))

  const submitLimits = () => {
    if (!limitTarget) return
    const kinds = limitRows.map((row) => row.limit_kind)
    if (kinds.some((kind) => !kind)) {
      message.error('每条都要选口径')
      return
    }
    if (new Set(kinds).size !== kinds.length) {
      message.error('同一种口径只能配一条')
      return
    }
    saveLimits.mutate({ target: limitTarget, limits: limitRows })
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
              <Tag
                key={item.key}
                color={item.usable ? 'purple' : 'default'}
                title={`限额：${limitText(item.model)}`}
              >
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

  // 已指定的那几个，排序跟 choices 一致（就是后端的选路顺序），列表上的序号才不骗人
  const pickedRows = choices.filter((item) => picked.includes(item.key))

  const providerOptions = (providers.data ?? []).map((provider) => ({
    value: provider.code,
    label: provider.enabled
      ? provider.name || provider.code
      : `${provider.name || provider.code}（停用）`,
  }))

  /** 选中 provider 下还没指定过的模型；默认只给能力对得上的。 */
  const modelOptions = choices
    .filter(
      (item) =>
        item.providerCode === providerCode &&
        !picked.includes(item.key) &&
        (showAll || !editing || item.capabilities.includes(editing.capability)),
    )
    .map((item) => ({
      value: item.key,
      label: `${item.modelId}（${item.capabilities.join('、')}）${item.usable ? '' : '［停用］'}`,
    }))

  /** 被能力过滤掉的数量，给那个展开开关当提示。 */
  const filteredOut = editing
    ? choices.filter(
        (item) =>
          item.providerCode === providerCode &&
          !picked.includes(item.key) &&
          !item.capabilities.includes(editing.capability),
      ).length
    : 0

  const add = () => {
    if (!candidate) return
    setPicked([...picked, candidate])
    setCandidate(null)
  }

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
        width={620}
        confirmLoading={save.isPending}
        onCancel={() => setEditing(null)}
        onOk={() => editing && save.mutate({ agentCode: editing.agent_code, keys: picked })}
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <List
            size="small"
            bordered
            header={
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {pickedRows.length
                  ? `已指定 ${pickedRows.length} 个，按这个顺序往下试`
                  : '已指定的模型'}
              </Typography.Text>
            }
            dataSource={pickedRows}
            locale={{ emptyText: '一个都没指定，这个 Agent 跑不起来' }}
            renderItem={(item, index) => (
              <List.Item
                actions={[
                  <Button key="limit" size="small" type="link" onClick={() => openLimits(item)}>
                    限额
                  </Button>,
                  <Button
                    key="remove"
                    size="small"
                    type="link"
                    danger
                    onClick={() => setPicked(picked.filter((key) => key !== item.key))}
                  >
                    移除
                  </Button>,
                ]}
              >
                <Space direction="vertical" size={0}>
                  <Space size={8}>
                    <Typography.Text type="secondary">{index + 1}</Typography.Text>
                    <Typography.Text>
                      {item.providerName} / {item.modelId}
                    </Typography.Text>
                    {!item.usable && <Tag>停用</Tag>}
                  </Space>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {limitText(item.model)}
                  </Typography.Text>
                </Space>
              </List.Item>
            )}
          />

          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              第一步：选 Provider
            </Typography.Text>
            <Select
              style={{ width: '100%' }}
              allowClear
              showSearch
              optionFilterProp="label"
              value={providerCode ?? undefined}
              placeholder={
                providerOptions.length
                  ? `选一个账号（共 ${providerOptions.length} 个，可打字筛）`
                  : '还没有账号，先去 Provider 配置页加'
              }
              disabled={providerOptions.length === 0}
              options={providerOptions}
              onChange={(code: string | undefined) => {
                setProviderCode(code ?? null)
                // 换了账号，刚选的模型不属于它了
                setCandidate(null)
              }}
            />
            <Typography.Text type="secondary" style={{ fontSize: 12, marginTop: 4 }}>
              第二步：选模型，一个一个加进上面的列表
            </Typography.Text>
            <Space.Compact style={{ width: '100%' }}>
              <Select
                style={{ flex: 1 }}
                showSearch
                optionFilterProp="label"
                value={candidate ?? undefined}
                placeholder={
                  !providerCode
                    ? '先选 Provider'
                    : modelOptions.length
                      ? `选一个模型（共 ${modelOptions.length} 个，可打字筛）`
                      : '这个账号没有可选的模型了'
                }
                disabled={!providerCode || modelOptions.length === 0}
                options={modelOptions}
                onChange={(key: string) => setCandidate(key)}
              />
              <Button type="primary" disabled={!candidate} onClick={add}>
                添加
              </Button>
            </Space.Compact>
            {editing && providerCode && (filteredOut > 0 || showAll) && (
              <Button
                size="small"
                type="link"
                style={{ paddingInline: 0, alignSelf: 'flex-start' }}
                onClick={() => setShowAll(!showAll)}
              >
                {showAll
                  ? `只看 ${editing.capability} 能力的模型`
                  : `还有 ${filteredOut} 个能力对不上的模型，也要看`}
              </Button>
            )}
          </Space>
        </Space>
      </Modal>

      <Modal
        open={limitTarget !== null}
        title={limitTarget ? `${limitTarget.providerName} / ${limitTarget.modelId} 的限额` : ''}
        okText="保存限额"
        width={780}
        confirmLoading={saveLimits.isPending}
        onCancel={() => setLimitTarget(null)}
        onOk={submitLimits}
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            限额算在模型头上，别的 Agent 用同一个模型也吃这份额度。每种口径最多一条，上限 0 =
            不限量；同一个分组名的多个模型共用一个窗口计数。这里存的是限额，跟上面的指定分两次保存。
          </Typography.Text>
          {limitRows.map((row, index) => (
            <Space key={index} align="center" style={{ display: 'flex' }}>
              <Select
                style={{ width: 170 }}
                placeholder="口径"
                value={row.limit_kind || undefined}
                options={(opts.data?.limit_kinds ?? []).map((kind) => ({
                  value: kind,
                  label: kindLabel(kind),
                }))}
                onChange={(kind: string) => patchLimit(index, { limit_kind: kind })}
              />
              <InputNumber
                style={{ width: 140 }}
                min={0}
                placeholder="上限，0=不限"
                value={row.max_value}
                onChange={(value) => patchLimit(index, { max_value: value ?? 0 })}
              />
              <Select
                style={{ width: 200 }}
                showSearch
                placeholder="day / day+11H"
                value={row.period_expr || undefined}
                options={Object.entries(opts.data?.period_examples ?? {}).map(([expr, hint]) => ({
                  value: expr,
                  label: `${expr} — ${hint}`,
                }))}
                onChange={(expr: string) => patchLimit(index, { period_expr: expr })}
              />
              <Input
                style={{ width: 120 }}
                placeholder="default"
                value={row.group_name}
                onChange={(event) => patchLimit(index, { group_name: event.target.value })}
              />
              <Tooltip title="删掉这条限额">
                <MinusCircleOutlined
                  onClick={() => setLimitRows(limitRows.filter((_, i) => i !== index))}
                />
              </Tooltip>
            </Space>
          ))}
          <Button
            block
            icon={<PlusOutlined />}
            onClick={() =>
              setLimitRows([
                ...limitRows,
                { limit_kind: 'calls', max_value: 0, period_expr: 'day', group_name: 'default' },
              ])
            }
          >
            加一条限额
          </Button>
        </Space>
      </Modal>
    </Space>
  )
}
