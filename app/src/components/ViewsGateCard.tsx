/**
 * 四视图与定稿。
 *
 * 这一步跟前两道门禁不一样：**没有裁决能放行**。`vision_reviewer` 只能拦——它说 APPROVE 也
 * 不推状态，四张定稿仍要用户在这里逐个指名。所以界面上「看图评审」与「定稿这一组」是两个按钮，
 * 中间没有自动衔接。
 *
 * 候选按视角分四栏列：用户重生的往往只有背面那一张，混在一条时间线里挑等于每次都要认一遍哪张
 * 是哪个面。每一栏默认选中最新那张，但定稿发的是他实际选中的四个 id，不是「各取最新」。
 *
 * 机器量出来的白底与画幅问题贴在缩略图上：这两项是像素统计题，模型判不准，而底不白、画幅不齐
 * 要到建模出网格才看得出来，那时候重来的代价是整条流水线。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Empty, Image, Input, Modal, Space, Tag, Typography } from 'antd'
import { useState } from 'react'

import {
  confirmViews,
  generateViews,
  listViews,
  renderImageUrl,
  reviewViews,
} from '@/api/characters'
import type { Character, Generation, ViewReview, ViewSet } from '@/types/api'

/** 四个视角与它们的人话说法。顺序跟后端一致：建模吃的是这个顺序。 */
const VARIANTS: { code: string; label: string }[] = [
  { code: 'front', label: '正面' },
  { code: 'right', label: '右侧 30°' },
  { code: 'back', label: '背面' },
  { code: 'left', label: '左侧 30°' },
]

const DECISION_COLORS: Record<string, string> = {
  APPROVE: 'green',
  CONCERNS: 'orange',
  REJECT: 'red',
}

interface Props {
  character: Character
}

export default function ViewsGateCard({ character }: Props) {
  const { message: toast } = App.useApp()
  const queryClient = useQueryClient()
  const [picks, setPicks] = useState<Record<string, string>>({})
  const [batch, setBatch] = useState<ViewSet | null>(null)
  const [verdict, setVerdict] = useState<ViewReview | null>(null)
  const [note, setNote] = useState('')
  const [asking, setAsking] = useState(false)

  const confirmed = Object.keys(character.view_paths).length > 0

  const views = useQuery({
    queryKey: ['character-views', character.id],
    queryFn: () => listViews(character.id),
  })
  const candidates = views.data ?? []

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['character', character.id] })
    void queryClient.invalidateQueries({ queryKey: ['character-events', character.id] })
    void queryClient.invalidateQueries({ queryKey: ['character-views', character.id] })
    void queryClient.invalidateQueries({ queryKey: ['characters'] })
  }

  const draw = useMutation({
    mutationFn: (variants: string[]) => generateViews(character.id, variants),
    onSuccess: (result) => {
      setBatch(result)
      // 新出的那几张直接选上：用户点了「重生背面」，要挑的就是这一张新的
      setPicks((before) => ({
        ...before,
        ...Object.fromEntries(result.images.map((one) => [one.variant, one.generation_id])),
      }))
      refresh()
      toast.success(`出了 ${result.images.length} 张，现在是「${result.state_label}」`)
    },
    onError: (err: Error) => toast.error(err.message),
  })

  const review = useMutation({
    mutationFn: () => reviewViews(character.id),
    onSuccess: (result) => {
      setVerdict(result)
      refresh()
      if (result.skipped) {
        toast.info('这个项目是 solo 模式，四视图不过审校，你自己看')
        return
      }
      toast.success(`审校说 ${result.decision}`)
    },
    onError: (err: Error) => toast.error(err.message),
  })

  const adopt = useMutation({
    mutationFn: () => confirmViews(character.id, chosen(candidates, picks), note.trim()),
    onSuccess: (result) => {
      setAsking(false)
      setNote('')
      refresh()
      toast.success(`四视图定稿了，现在是「${result.state_label}」`)
    },
    onError: (err: Error) => toast.error(err.message),
  })

  const ready = VARIANTS.every((one) => pickOf(candidates, picks, one.code) !== null)
  const busy = draw.isPending || review.isPending || adopt.isPending

  return (
    <Card
      size="small"
      title="四视图与定稿"
      extra={
        <Space size={8}>
          <Button
            loading={review.isPending}
            disabled={candidates.length === 0 || busy}
            onClick={() => review.mutate()}
          >
            让审校看图
          </Button>
          <Button
            type="primary"
            loading={draw.isPending}
            disabled={busy}
            onClick={() => draw.mutate([])}
          >
            {candidates.length === 0 ? '生成四视图' : '四张重出'}
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          参考图是姿势模版加定稿渲染图，四张并发出图，一律落在 <code>tmp/</code> 里。审校只能拦
          不能放行——定稿这一组要你自己按。
        </Typography.Text>

        {batch?.size_complaint != null && (
          <Alert type="warning" showIcon message="画幅不齐" description={batch.size_complaint} />
        )}
        {batch?.failures.map((one) => (
          <Alert
            key={one.variant}
            type="error"
            showIcon
            message={`${one.label}这张没出来`}
            description={
              <Space direction="vertical" size={4}>
                <Typography.Text style={{ fontSize: 13 }}>{one.reason}</Typography.Text>
                <Button size="small" disabled={busy} onClick={() => draw.mutate([one.variant])}>
                  只重生这一张
                </Button>
              </Space>
            }
          />
        ))}

        {candidates.length === 0 ? (
          <Empty image={null} description="还没有出过四视图。生成之后候选会按视角分栏留在这里。" />
        ) : (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            {VARIANTS.map((one) => (
              <Lane
                key={one.code}
                characterId={character.id}
                label={one.label}
                rows={candidates.filter((row) => row.variant === one.code)}
                problems={batch?.images.find((img) => img.variant === one.code)?.problems ?? []}
                selected={pickOf(candidates, picks, one.code)}
                busy={busy}
                onSelect={(id) => setPicks((before) => ({ ...before, [one.code]: id }))}
                onRedraw={() => draw.mutate([one.code])}
              />
            ))}
          </Space>
        )}

        {verdict !== null && <VerdictView review={verdict} />}

        {confirmed ? (
          <Alert
            type="success"
            showIcon
            message="四视图已定稿"
            description={
              <Space direction="vertical" size={2}>
                {Object.entries(character.view_paths).map(([code, path]) => (
                  <Typography.Text key={code} style={{ fontSize: 12 }}>
                    {labelOf(code)}：{path}
                  </Typography.Text>
                ))}
              </Space>
            }
          />
        ) : (
          candidates.length > 0 && (
            <>
              <Alert
                type="info"
                message="四个角度要一起定"
                description={
                  <Typography.Text style={{ fontSize: 13 }}>
                    建模吃的是一整组图，一组里两张新两张旧出来的模型是错的却看不出为什么。每一栏
                    挑好之后再按定稿。
                  </Typography.Text>
                }
              />
              <Button
                type="primary"
                disabled={!ready || busy}
                onClick={() => {
                  setNote('')
                  setAsking(true)
                }}
              >
                {ready ? '定稿这一组' : '四个角度都挑齐才能定稿'}
              </Button>
            </>
          )
        )}
      </Space>

      <Modal
        open={asking}
        title="把这一组定为四视图"
        okText="就这一组"
        cancelText="再想想"
        confirmLoading={adopt.isPending}
        onCancel={() => setAsking(false)}
        onOk={() => adopt.mutate()}
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            这四张会拷到定稿位，建模那一步只吃它们。留一句你凭什么认可这一组，日后回看能对上。
          </Typography.Text>
          <Input.TextArea
            rows={3}
            value={note}
            placeholder="例：四个面的尾巴都是两条且分开，腰带位置也对得上"
            onChange={(event) => setNote(event.target.value)}
          />
        </Space>
      </Modal>
    </Card>
  )
}

/** 某个视角选中的那一张：用户挑过就用他挑的，没挑过默认最新一张。 */
function pickOf(rows: Generation[], picks: Record<string, string>, code: string): string | null {
  const mine = rows.filter((one) => one.variant === code)
  const picked = picks[code]
  if (picked !== undefined && mine.some((one) => one.id === picked)) return picked
  return mine[0]?.id ?? null
}

function chosen(rows: Generation[], picks: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const one of VARIANTS) {
    const id = pickOf(rows, picks, one.code)
    if (id !== null) out[one.code] = id
  }
  return out
}

function labelOf(code: string): string {
  return VARIANTS.find((one) => one.code === code)?.label ?? code
}

/** 一个视角一栏：这个面的候选横着排，选中的描边，机器量出来的病贴在栏头。 */
function Lane({
  characterId,
  label,
  rows,
  problems,
  selected,
  busy,
  onSelect,
  onRedraw,
}: {
  characterId: string
  label: string
  rows: Generation[]
  problems: string[]
  selected: string | null
  busy: boolean
  onSelect: (id: string) => void
  onRedraw: () => void
}) {
  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Space size={6} wrap>
        <Typography.Text strong style={{ fontSize: 13 }}>
          {label}
        </Typography.Text>
        {rows.length === 0 && <Tag>还没有</Tag>}
        {problems.map((one) => (
          <Tag key={one} color="volcano">
            {one}
          </Tag>
        ))}
        <Button size="small" type="link" disabled={busy} onClick={onRedraw}>
          重生这一张
        </Button>
      </Space>
      <Space size={8} wrap>
        {rows.map((row) => (
          <Thumb
            key={row.id}
            characterId={characterId}
            row={row}
            active={row.id === selected}
            onSelect={() => onSelect(row.id)}
          />
        ))}
      </Space>
    </Space>
  )
}

function Thumb({
  characterId,
  row,
  active,
  onSelect,
}: {
  characterId: string
  row: Generation
  active: boolean
  onSelect: () => void
}) {
  // 图本体走 `<img src>`：一张 4K 图转 base64 塞进 JSON 再膨 33%，而浏览器对地址本来就会缓存
  const url = useQuery({
    queryKey: ['render-url', characterId, row.id],
    queryFn: () => renderImageUrl(characterId, row.id),
    staleTime: Infinity,
  })

  return (
    <div
      onClick={onSelect}
      style={{
        border: `2px solid ${active ? '#1677ff' : 'transparent'}`,
        borderRadius: 6,
        padding: 2,
        cursor: 'pointer',
      }}
    >
      <Space direction="vertical" size={2}>
        {url.data === undefined ? (
          <div style={{ width: 108, height: 108, background: '#f5f5f5', borderRadius: 4 }} />
        ) : (
          <Image src={url.data} width={108} height={108} style={{ objectFit: 'cover' }} />
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

/**
 * 审校说了什么。分节理由摊开，底下挂一份全文。
 *
 * 全文要留着：摘成一句「REJECT 1 处」就把判断依据丢了，用户看不出该改 prompt 还是该换姿势模版。
 */
function VerdictView({ review }: { review: ViewReview }) {
  if (review.skipped) {
    return (
      <Alert
        type="info"
        showIcon
        message="这个项目不过审校"
        description="项目配的是 solo 模式，四视图由你自己看。想让平台一起看，去项目设置里改评审粒度。"
      />
    )
  }

  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Space size={6} wrap>
        <Tag color={DECISION_COLORS[review.decision] ?? 'default'}>{review.decision}</Tag>
        <Tag>粒度 {review.mode}</Tag>
        <Tag>第 {review.attempt} 轮</Tag>
        {review.regenerated > 0 && <Tag color="blue">自动重生 {review.regenerated} 次</Tag>}
      </Space>
      {review.manual && (
        <Alert
          type="warning"
          showIcon
          message="自动重生用尽了还是没过"
          description="接下来得人来看：连着几次过不了的问题，多半要改 prompt 或换姿势模版才解得开。"
        />
      )}
      {review.verdicts.map((one, index) => (
        <Space
          key={`${one.variants.join('-')}-${index}`}
          direction="vertical"
          size={2}
          style={{ width: '100%' }}
        >
          <Space size={6}>
            <Tag color={DECISION_COLORS[one.decision] ?? 'default'}>{one.decision}</Tag>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {one.variants.map(labelOf).join('、')}
            </Typography.Text>
          </Space>
          <Typography.Paragraph
            style={{ fontSize: 12, marginBottom: 0 }}
            ellipsis={{ rows: 4, expandable: true, symbol: '看裁决原文' }}
          >
            <span style={{ whiteSpace: 'pre-wrap' }}>{one.text}</span>
          </Typography.Paragraph>
        </Space>
      ))}
    </Space>
  )
}
