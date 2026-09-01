/**
 * 设定评审与门禁 1。
 *
 * 这张卡片把两件事分开摆：上半是 `spec_reviewer` 的裁决（意见），下半是人工门禁（放行）。
 * 哪怕裁决是 `APPROVE`，「采用定稿」也得用户自己按——自动裁决替人拍板，等于把责任推给一个
 * 看不见全局的模型。
 *
 * 门禁的交互是「先阐述、再捕获」：先把这一步意味着什么说清楚，再让用户在四个固定选项里选。
 * 直接甩四个按钮的话，用户按之前并不知道「采用定稿」会锁住后面每一张图的依据。
 *
 * 四个选项都要理由，且理由都落进 `task_events`。事后要回答「这份定稿当时凭什么过的」，只有
 * 这条时间线答得上。
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Descriptions, Input, Modal, Space, Tag, Typography } from 'antd'
import { useState } from 'react'

import { confirmSpec, rejectSpec, reviewSpec } from '@/api/characters'
import type { Handoff } from '@/components/chat'
import type { Character, SpecReview } from '@/types/api'

/** 裁决 token → 颜色。`CONCERNS` 不是通过，但也不是推翻，所以单独一档。 */
const DECISION_COLORS: Record<string, string> = {
  APPROVE: 'green',
  CONCERNS: 'orange',
  REJECT: 'red',
}

type GateKind = 'adopt' | 'revise' | 'pivot' | 'stop'

interface Gate {
  kind: GateKind
  title: string
  /** 输入框上方那句话：告诉用户这一栏该写什么，而不是笼统的「请输入」。 */
  hint: string
  placeholder: string
  /** 驳回类选项必须写清哪里不行，否则下一轮设定会话拿不到可用的输入。 */
  required: boolean
  danger?: boolean
}

const GATES: Record<GateKind, Gate> = {
  adopt: {
    kind: 'adopt',
    title: '采用这一版定稿',
    hint: '这一版就是后面渲染图、四视图、模型的唯一依据。留一句你凭什么认可它，日后回看能对上。',
    placeholder: '例：四肢比例与视觉规范一致，环境设定也补齐了',
    required: false,
  },
  revise: {
    kind: 'revise',
    title: '只改某一项，其余保留',
    hint: '写清哪一项要改成什么。其余已经谈定的内容会留着，写手只动你指出的这一处。',
    placeholder: '例：尾巴改成 2 条且彼此分离，其余不动',
    required: true,
  },
  pivot: {
    kind: 'pivot',
    title: '换个方向重来',
    hint: '整版推翻，会另开一场设定会话——旧会话的上下文会把模型拉回原来的方向。',
    placeholder: '例：不要机械改造路线，改成纯生物形态的兽人',
    required: true,
    danger: true,
  },
  stop: {
    kind: 'stop',
    title: '先停在这里',
    hint: '状态停在「设定对焦中」，理由记进时间线。下次接着聊时能看见这次为什么停下。',
    placeholder: '例：等美术给参考图之后再定',
    required: true,
  },
}

interface Props {
  projectCode: string
  character: Character
  /** 当下那场设定会话；`REJECT` 后要靠它承载自动重生的几轮。 */
  conversationId: string | null
  /** 把「下一轮要说的话」递回会话面板，由用户过目后发出。 */
  onHandoff: (handoff: Handoff) => void
}

export default function SpecGateCard({ projectCode, character, conversationId, onHandoff }: Props) {
  const { message: toast, modal } = App.useApp()
  const queryClient = useQueryClient()
  const [verdict, setVerdict] = useState<SpecReview | null>(null)
  const [gate, setGate] = useState<Gate | null>(null)
  const [note, setNote] = useState('')

  const confirmed = character.gate_spec_confirmed_at !== null
  const settled = character.spec_path !== null

  const refresh = () => {
    void queryClient.invalidateQueries({
      queryKey: ['project', projectCode, 'character', character.id],
    })
    void queryClient.invalidateQueries({
      queryKey: ['project', projectCode, 'character-events', character.id],
    })
    void queryClient.invalidateQueries({ queryKey: ['project', projectCode, 'characters'] })
  }

  const review = useMutation({
    mutationFn: () => reviewSpec(projectCode, character.id, conversationId),
    onSuccess: (result) => {
      setVerdict(result)
      refresh()
      if (result.regenerated > 0) {
        void queryClient.invalidateQueries({
          queryKey: ['project', projectCode, 'conversation', conversationId],
        })
        toast.info(`驳回了 ${result.regenerated} 次，写手已按理由重生，会话里能看到这几轮`)
      }
      if (result.manual) {
        toast.warning('自动重生用尽还是没过，接下来只能你自己判断')
      }
    },
    onError: (err: Error) => toast.error(err.message),
  })

  const decide = useMutation({
    mutationFn: async (chosen: Gate) => {
      const reason = note.trim()
      if (chosen.kind === 'adopt') return confirmSpec(projectCode, character.id, reason)
      await rejectSpec(projectCode, character.id, reason)
      return null
    },
    onSuccess: (result, chosen) => {
      const reason = note.trim()
      setGate(null)
      setNote('')
      refresh()
      if (result !== null) {
        toast.success(`已采用定稿，现在是「${result.state_label}」`)
        return
      }
      if (chosen.kind === 'stop') {
        toast.success('已停下，理由记进时间线了')
        return
      }
      const text =
        chosen.kind === 'revise' ? `上一版这一项要改：${reason}` : `换个方向重来：${reason}`
      // 换方向也不另起一场：一个角色就一场会话，新方向靠递进去那段话说清
      onHandoff({ text, nonce: Date.now() })
      toast.info('已记下。左边会话里帮你拟好了这句话，看一眼再发')
    },
    onError: (err: Error) => toast.error(err.message),
  })

  const open = (chosen: Gate) => {
    if (chosen.kind === 'adopt' && !settled) {
      modal.info({
        title: '还没有可确认的定稿',
        content: '门禁确认的是磁盘上那一份。先在会话里把草稿「确认沉淀」，再回来采用。',
      })
      return
    }
    setNote('')
    setGate(chosen)
  }

  return (
    <Card
      size="small"
      title="设定评审与门禁"
      extra={
        <Button
          type={verdict === null ? 'primary' : 'default'}
          loading={review.isPending}
          onClick={() => review.mutate()}
        >
          {verdict === null ? '让评审看一版' : '再审一次'}
        </Button>
      }
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {verdict === null ? (
          <Typography.Text type="secondary">
            评审会挑出缺失的维度并抽出硬性约束清单。它只给意见，放行仍是你的事。
          </Typography.Text>
        ) : (
          <Verdict result={verdict} />
        )}

        {character.hard_constraints.length > 0 && (
          <Descriptions
            size="small"
            column={1}
            bordered
            title="硬性约束清单"
            items={character.hard_constraints.map((one, index) => ({
              key: String(index),
              label: one.item,
              children: one.value,
            }))}
          />
        )}

        {confirmed ? (
          <Alert
            type="success"
            showIcon
            message={`设定已确认（${character.gate_spec_confirmed_at?.replace('T', ' ').slice(0, 19)}）`}
            description="后面每一步都以这份定稿为依据。要改就回到会话里重开一版，再走一次这道门禁。"
          />
        ) : (
          <>
            <Alert
              type="info"
              message="这一步只有你能拍板"
              description={
                <Typography.Text style={{ fontSize: 13 }}>
                  采用之后，渲染图、四视图、模型都按这一版生成，中途改设定要从这里重走。
                  {settled ? '' : '现在磁盘上还没有定稿——先在会话里把草稿「确认沉淀」。'}
                </Typography.Text>
              }
            />
            <Space wrap>
              <Button type="primary" disabled={!settled} onClick={() => open(GATES.adopt)}>
                采用定稿
              </Button>
              <Button onClick={() => open(GATES.revise)}>改某一项重生</Button>
              <Button onClick={() => open(GATES.pivot)}>换方向重生</Button>
              <Button onClick={() => open(GATES.stop)}>停在这里</Button>
            </Space>
          </>
        )}
      </Space>

      <Modal
        open={gate !== null}
        title={gate?.title}
        okText="就这样"
        cancelText="再想想"
        okButtonProps={{
          danger: gate?.danger,
          disabled: gate?.required === true && note.trim() === '',
        }}
        confirmLoading={decide.isPending}
        onCancel={() => setGate(null)}
        onOk={() => gate && decide.mutate(gate)}
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            {gate?.hint}
          </Typography.Text>
          <Input.TextArea
            rows={4}
            value={note}
            placeholder={gate?.placeholder}
            onChange={(event) => setNote(event.target.value)}
          />
        </Space>
      </Modal>
    </Card>
  )
}

/**
 * 裁决展示。分节理由给条目，底下再挂一份全文。
 *
 * 全文要留着：分节是平台按格式抽的，抽漏了的那句话往往正是判断依据。
 */
function Verdict({ result }: { result: SpecReview }) {
  const sections = Object.entries(result.sections)

  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Space size={6} wrap>
        <Tag color={DECISION_COLORS[result.decision] ?? 'default'}>{result.decision}</Tag>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          第 {result.attempt} 轮{result.regenerated > 0 && ` · 自动重生 ${result.regenerated} 次`}
        </Typography.Text>
        {result.manual && <Tag color="volcano">转人工</Tag>}
      </Space>
      {sections.map(([name, items]) => (
        <div key={name}>
          <Typography.Text strong style={{ fontSize: 13 }}>
            {name}
          </Typography.Text>
          <ul style={{ margin: '4px 0 0', paddingLeft: 20, fontSize: 13 }}>
            {items.map((one, index) => (
              <li key={index}>{one}</li>
            ))}
          </ul>
        </div>
      ))}
      <Typography.Paragraph
        type="secondary"
        style={{ fontSize: 12, marginBottom: 0 }}
        ellipsis={{ rows: 2, expandable: true, symbol: '看裁决全文' }}
      >
        <span style={{ whiteSpace: 'pre-wrap' }}>{result.text}</span>
      </Typography.Paragraph>
    </Space>
  )
}
