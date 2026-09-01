/**
 * 渲染图与门禁 2。
 *
 * 排布跟设定那一步是一样的逻辑：上半是产物（卡片 + 候选图），下半是人工门禁。生成不等于定稿
 * ——产物一律落在 `tmp/` 里，定稿位要等用户在这里指着某一张说「就它」。
 *
 * 候选全列出来而不是只留最新一张：用户连生几张再挑是常态，只显示最新的话「上一张其实更好」
 * 就找不回来了。采用时必须指名哪一张，默认「最新」在连生之后就不是他指的那张。
 *
 * 「改某一项重生」与「换方向重生」都直接落到生图上：这一步没有会话可承载，写手拿到的就是这
 * 句话本身——所以只改一项时要把「哪一项」单独收下来，其余字段与 prompt 的其他层原样留着。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Empty, Image, Input, Modal, Space, Tag, Typography } from 'antd'
import { useState } from 'react'

import {
  confirmRender,
  draftAssetSpec,
  listRenders,
  rejectRender,
  renderCharacter,
  renderImageUrl,
} from '@/api/characters'
import type { AssetSpec, Character, Generation } from '@/types/api'

type GateKind = 'adopt' | 'revise' | 'pivot' | 'stop'

interface Gate {
  kind: GateKind
  title: string
  /** 输入框上方那句话：告诉用户这一栏该写什么，而不是笼统的「请输入」。 */
  hint: string
  placeholder: string
  required: boolean
  /** 只改某一项时要先说清是哪一项，否则写手只能整张重写。 */
  needField?: boolean
  danger?: boolean
}

const GATES: Record<GateKind, Gate> = {
  adopt: {
    kind: 'adopt',
    title: '采用这一张定稿',
    hint: '这一张会拷到定稿位，四视图与模型都以它为准。留一句你凭什么认可它，日后回看能对上。',
    placeholder: '例：双尾分离清楚，冷光调也对上了视觉规范',
    required: false,
  },
  revise: {
    kind: 'revise',
    title: '只改某一项，重新出图',
    hint: '先说清哪一项要改。卡片里其余字段与 prompt 的其他层会原样留着，只动你指出的这一处。',
    placeholder: '例：改成侧顶光，脸部不要压这么暗',
    required: true,
    needField: true,
  },
  pivot: {
    kind: 'pivot',
    title: '换个方向重新出图',
    hint: '整张卡片重做。适合方向本身不对——比如姿态、构图或气质要换，而不只是某一处细节。',
    placeholder: '例：不要站姿，改成蹲伏准备扑击的姿态',
    required: true,
    danger: true,
  },
  stop: {
    kind: 'stop',
    title: '先停在这里',
    hint: '状态停在「渲染图已生成」，理由记进时间线。下次接着做时能看见这次为什么停下。',
    placeholder: '例：等策划确认配色之后再定',
    required: true,
  },
}

interface Props {
  projectCode: string
  character: Character
}

export default function RenderGateCard({ projectCode, character }: Props) {
  const { message: toast } = App.useApp()
  const queryClient = useQueryClient()
  const [gate, setGate] = useState<Gate | null>(null)
  const [note, setNote] = useState('')
  const [field, setField] = useState('')
  const [chosen, setChosen] = useState<string | null>(null)
  const [spec, setSpec] = useState<AssetSpec | null>(null)

  const confirmed = character.gate_render_confirmed_at !== null

  const renders = useQuery({
    queryKey: ['project', projectCode, 'character-renders', character.id],
    queryFn: () => listRenders(projectCode, character.id),
  })
  const candidates = renders.data ?? []
  const selected = chosen ?? candidates[0]?.id ?? null

  const refresh = () => {
    void queryClient.invalidateQueries({
      queryKey: ['project', projectCode, 'character', character.id],
    })
    void queryClient.invalidateQueries({
      queryKey: ['project', projectCode, 'character-events', character.id],
    })
    void queryClient.invalidateQueries({
      queryKey: ['project', projectCode, 'character-renders', character.id],
    })
    void queryClient.invalidateQueries({ queryKey: ['project', projectCode, 'characters'] })
  }

  const preview = useMutation({
    mutationFn: () => draftAssetSpec(projectCode, character.id),
    onSuccess: (result) => {
      setSpec(result)
      toast.success('卡片出来了，看一眼再生图')
    },
    onError: (err: Error) => toast.error(err.message),
  })

  const draw = useMutation({
    mutationFn: (input: { note: string; field: string }) =>
      renderCharacter(projectCode, character.id, input.note, input.field),
    onSuccess: (result) => {
      setSpec(result.spec)
      setChosen(result.generation_id)
      refresh()
      toast.success(`出了一张 ${result.width}x${result.height}，落在 ${result.file_path}`)
    },
    onError: (err: Error) => toast.error(err.message),
  })

  const decide = useMutation({
    mutationFn: async (which: Gate) => {
      const reason = note.trim()
      if (which.kind === 'adopt') {
        if (selected === null) throw new Error('先选中要采用的那一张')
        return confirmRender(projectCode, character.id, selected, reason)
      }
      if (which.kind === 'stop') return rejectRender(projectCode, character.id, reason)
      // 驳回的理由先记下来，再按这句话重出一张——不记的话时间线上就只剩下一张新图
      await rejectRender(projectCode, character.id, reason)
      await draw.mutateAsync({ note: reason, field: which.kind === 'revise' ? field.trim() : '' })
      return null
    },
    onSuccess: (result) => {
      setGate(null)
      setNote('')
      setField('')
      refresh()
      if (result === null) return
      toast.success(`已采用这一张，现在是「${result.state_label}」`)
    },
    onError: (err: Error) => toast.error(err.message),
  })

  const open = (which: Gate) => {
    setNote('')
    setField('')
    setGate(which)
  }

  const busy = draw.isPending || decide.isPending

  return (
    <Card
      size="small"
      title="渲染图与门禁"
      extra={
        <Space size={8}>
          <Button loading={preview.isPending} onClick={() => preview.mutate()}>
            先看卡片
          </Button>
          <Button
            type="primary"
            loading={draw.isPending}
            disabled={confirmed}
            onClick={() => draw.mutate({ note: '', field: '' })}
          >
            {candidates.length === 0 ? '生成一张' : '再来一张'}
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          卡片是这张图唯一的规格，生成的图一律落在 <code>tmp/</code> 里。定稿位要等你在下面采用。
        </Typography.Text>

        {spec !== null && <SpecView spec={spec} />}

        {candidates.length === 0 ? (
          <Empty image={null} description="还没有出过图。生成之后候选都会留在这里。" />
        ) : (
          <Candidates
            projectCode={projectCode}
            characterId={character.id}
            rows={candidates}
            selected={selected}
            onSelect={setChosen}
          />
        )}

        {confirmed ? (
          <Alert
            type="success"
            showIcon
            message={`渲染图已定稿（${character.gate_render_confirmed_at?.replace('T', ' ').slice(0, 19)}）`}
            description={
              <Typography.Text style={{ fontSize: 13 }}>
                定稿在 {character.render_path ?? '定稿位'}。四视图会以它为唯一参考图，候选图仍然留在
                <code>tmp/</code> 里。
              </Typography.Text>
            }
          />
        ) : (
          candidates.length > 0 && (
            <>
              <Alert
                type="info"
                message="这一步只有你能拍板"
                description={
                  <Typography.Text style={{ fontSize: 13 }}>
                    采用之后，四视图与模型都以这一张为参考图。候选图不会被删，过两天想换回来还找得到。
                  </Typography.Text>
                }
              />
              <Space wrap>
                <Button
                  type="primary"
                  disabled={selected === null || busy}
                  onClick={() => open(GATES.adopt)}
                >
                  采用定稿
                </Button>
                <Button disabled={busy} onClick={() => open(GATES.revise)}>
                  改某一项重生
                </Button>
                <Button disabled={busy} onClick={() => open(GATES.pivot)}>
                  换方向重生
                </Button>
                <Button disabled={busy} onClick={() => open(GATES.stop)}>
                  停在这里
                </Button>
              </Space>
            </>
          )
        )}
      </Space>

      <Modal
        open={gate !== null}
        title={gate?.title}
        okText="就这样"
        cancelText="再想想"
        okButtonProps={{
          danger: gate?.danger,
          disabled:
            (gate?.required === true && note.trim() === '') ||
            (gate?.needField === true && field.trim() === ''),
        }}
        confirmLoading={decide.isPending}
        onCancel={() => setGate(null)}
        onOk={() => gate && decide.mutate(gate)}
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            {gate?.hint}
          </Typography.Text>
          {gate?.needField === true && (
            <Input
              value={field}
              placeholder="要改的是哪一项？例：光照 / 姿态 / 构图"
              onChange={(event) => setField(event.target.value)}
            />
          )}
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
 * 卡片展示。分字段列出来，底下再挂一份原文。
 *
 * 原文要留着：分字段是平台按格式抽的，抽漏了的那一行往往正是图不对的原因。
 */
function SpecView({ spec }: { spec: AssetSpec }) {
  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Space size={6} wrap>
        <Tag color="blue">{spec.code}</Tag>
        {spec.size !== '' && <Tag>{spec.size}</Tag>}
        {spec.constraints.map((one) => (
          <Tag key={one} color="volcano">
            {one}
          </Tag>
        ))}
      </Space>
      <Typography.Paragraph
        style={{ fontSize: 12, marginBottom: 0 }}
        ellipsis={{ rows: 3, expandable: true, symbol: '看卡片原文' }}
      >
        <span style={{ whiteSpace: 'pre-wrap' }}>{spec.card}</span>
      </Typography.Paragraph>
    </Space>
  )
}

/** 候选图。选中的那一张描边，定稿的那一张挂个标——门禁要在这几张之间挑。 */
function Candidates({
  projectCode,
  characterId,
  rows,
  selected,
  onSelect,
}: {
  projectCode: string
  characterId: string
  rows: Generation[]
  selected: string | null
  onSelect: (id: string) => void
}) {
  return (
    <Space size={8} wrap>
      {rows.map((row) => (
        <Thumb
          key={row.id}
          projectCode={projectCode}
          characterId={characterId}
          row={row}
          active={row.id === selected}
          onSelect={onSelect}
        />
      ))}
    </Space>
  )
}

function Thumb({
  projectCode,
  characterId,
  row,
  active,
  onSelect,
}: {
  projectCode: string
  characterId: string
  row: Generation
  active: boolean
  onSelect: (id: string) => void
}) {
  const url = useQuery({
    queryKey: ['project', projectCode, 'render-url', characterId, row.id],
    queryFn: () => renderImageUrl(projectCode, characterId, row.id),
    staleTime: Infinity,
  })

  return (
    <div
      onClick={() => onSelect(row.id)}
      style={{
        border: `2px solid ${active ? '#1677ff' : 'transparent'}`,
        borderRadius: 6,
        padding: 2,
        cursor: 'pointer',
      }}
    >
      <Space direction="vertical" size={2}>
        {url.data === undefined ? (
          <div style={{ width: 132, height: 132, background: '#f5f5f5', borderRadius: 4 }} />
        ) : (
          <Image src={url.data} width={132} height={132} style={{ objectFit: 'cover' }} />
        )}
        <Space size={4}>
          {row.is_final && <Tag color="green">定稿</Tag>}
          <Typography.Text type="secondary" style={{ fontSize: 11 }}>
            {row.created_at.replace('T', ' ').slice(5, 16)}
          </Typography.Text>
        </Space>
      </Space>
    </div>
  )
}
