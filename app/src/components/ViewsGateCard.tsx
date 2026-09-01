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

const DECISION_COLORS: Record<string, string> = {
  APPROVE: 'green',
  CONCERNS: 'orange',
  REJECT: 'red',
}

interface Props {
  projectCode: string
  character: Character
}

export default function ViewsGateCard({ projectCode, character }: Props) {
  const { message: toast } = App.useApp()
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<string | null>(null)
  const [batch, setBatch] = useState<ViewSet | null>(null)
  const [verdict, setVerdict] = useState<ViewReview | null>(null)
  const [note, setNote] = useState('')
  const [asking, setAsking] = useState(false)

  const confirmed = Object.keys(character.view_paths).length > 0
  const views = useQuery({
    queryKey: ['project', projectCode, 'character-views', character.id],
    queryFn: () => listViews(projectCode, character.id),
  })
  const all = views.data ?? []
  const candidates = all.filter((one) => one.variant === 'sheet')
  const legacy = all.filter((one) => one.variant !== 'sheet')
  const picked =
    selected !== null && candidates.some((one) => one.id === selected)
      ? selected
      : (candidates[0]?.id ?? null)

  const refresh = () => {
    void queryClient.invalidateQueries({
      queryKey: ['project', projectCode, 'character', character.id],
    })
    void queryClient.invalidateQueries({
      queryKey: ['project', projectCode, 'character-events', character.id],
    })
    void queryClient.invalidateQueries({
      queryKey: ['project', projectCode, 'character-views', character.id],
    })
    void queryClient.invalidateQueries({ queryKey: ['project', projectCode, 'characters'] })
  }

  const draw = useMutation({
    mutationFn: () => generateViews(projectCode, character.id),
    onSuccess: (result) => {
      setBatch(result)
      const image = result.images[0]
      if (image !== undefined) setSelected(image.generation_id)
      refresh()
      if (image !== undefined) toast.success(`四视图已生成，现在是「${result.state_label}」`)
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const review = useMutation({
    mutationFn: () => reviewViews(projectCode, character.id),
    onSuccess: (result) => {
      setVerdict(result)
      refresh()
      if (result.skipped) toast.info('这个项目是 solo 模式，四视图由你自己看')
      else toast.success(`审校说 ${result.decision}`)
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const adopt = useMutation({
    mutationFn: () => confirmViews(projectCode, character.id, { sheet: picked! }, note.trim()),
    onSuccess: (result) => {
      setAsking(false)
      setNote('')
      refresh()
      toast.success(`四视图定稿了，现在是「${result.state_label}」`)
    },
    onError: (error: Error) => toast.error(error.message),
  })

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
            onClick={() => draw.mutate()}
          >
            {candidates.length === 0 ? '生成四视图' : '整张重出'}
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          单张 2048×2048 四宫格：左上正面、右上右侧 30°、左下背面、右下左侧 30°。
        </Typography.Text>

        {batch?.failures.map((one) => (
          <Alert
            key={one.variant}
            type="error"
            showIcon
            message="四视图生成失败"
            description={one.reason}
          />
        ))}

        {candidates.length === 0 ? (
          <Empty image={null} description="还没有新版四视图四宫格。" />
        ) : (
          <Space size={10} wrap>
            {candidates.map((row) => (
              <Thumb
                key={row.id}
                projectCode={projectCode}
                characterId={character.id}
                row={row}
                active={row.id === picked}
                problems={
                  batch?.images.find((image) => image.generation_id === row.id)?.problems ?? []
                }
                onSelect={() => setSelected(row.id)}
              />
            ))}
          </Space>
        )}

        {legacy.length > 0 && (
          <Alert
            type="info"
            showIcon
            message="旧版四张分图"
            description="旧候选继续保留，只能查看；需要定稿时请重新生成一张四宫格。"
          />
        )}

        {verdict !== null && <VerdictView review={verdict} />}

        {confirmed ? (
          <Alert
            type="success"
            showIcon
            message="四视图已定稿"
            description={Object.entries(character.view_paths).map(([code, path]) => (
              <Typography.Text key={code} style={{ display: 'block', fontSize: 12 }}>
                {code === 'sheet' ? '四宫格' : `旧版 ${code}`}：{path}
              </Typography.Text>
            ))}
          />
        ) : (
          candidates.length > 0 && (
            <Button
              type="primary"
              disabled={picked === null || busy}
              onClick={() => {
                setNote('')
                setAsking(true)
              }}
            >
              定稿这张四视图
            </Button>
          )
        )}
      </Space>

      <Modal
        open={asking}
        title="把这张四宫格定为四视图"
        okText="就这张"
        cancelText="再想想"
        confirmLoading={adopt.isPending}
        onCancel={() => setAsking(false)}
        onOk={() => adopt.mutate()}
      >
        <Input.TextArea
          rows={3}
          value={note}
          placeholder="例：四个位置视角正确，造型与定稿效果图一致"
          onChange={(event) => setNote(event.target.value)}
        />
      </Modal>
    </Card>
  )
}

function Thumb({
  projectCode,
  characterId,
  row,
  active,
  problems,
  onSelect,
}: {
  projectCode: string
  characterId: string
  row: Generation
  active: boolean
  problems: string[]
  onSelect: () => void
}) {
  const url = useQuery({
    queryKey: ['project', projectCode, 'render-url', characterId, row.id],
    queryFn: () => renderImageUrl(projectCode, characterId, row.id),
    staleTime: Infinity,
  })

  return (
    <div
      onClick={onSelect}
      style={{
        width: 244,
        border: `2px solid ${active ? '#1677ff' : 'transparent'}`,
        borderRadius: 6,
        padding: 3,
        cursor: 'pointer',
      }}
    >
      <Space direction="vertical" size={4} style={{ width: '100%' }}>
        {url.data === undefined ? (
          <div style={{ width: 234, height: 234, background: '#f5f5f5', borderRadius: 4 }} />
        ) : (
          <Image
            preview={false}
            src={url.data}
            width={234}
            height={234}
            style={{ objectFit: 'cover' }}
          />
        )}
        <Space size={4} wrap>
          {row.is_final && <Tag color="green">定稿</Tag>}
          <Typography.Text type="secondary" style={{ fontSize: 11 }}>
            {row.created_at.replace('T', ' ').slice(5, 16)}
          </Typography.Text>
        </Space>
        {problems.map((one) => (
          <Tag key={one} color="volcano">
            {one}
          </Tag>
        ))}
      </Space>
    </div>
  )
}

function VerdictView({ review }: { review: ViewReview }) {
  if (review.skipped) {
    return (
      <Alert type="info" showIcon message="这个项目不过审校" description="四视图由你自己确认。" />
    )
  }
  return (
    <Space direction="vertical" size={6} style={{ width: '100%' }}>
      <Space size={6} wrap>
        <Tag color={DECISION_COLORS[review.decision] ?? 'default'}>{review.decision}</Tag>
        <Tag>第 {review.attempt} 轮</Tag>
        {review.regenerated > 0 && <Tag color="blue">整张重生 {review.regenerated} 次</Tag>}
      </Space>
      {review.manual && <Alert type="warning" showIcon message="自动重生用尽，请人工检查" />}
      {review.verdicts.map((one, index) => (
        <Typography.Paragraph
          key={`${one.variants.join('-')}-${index}`}
          style={{ fontSize: 12, marginBottom: 0, whiteSpace: 'pre-wrap' }}
          ellipsis={{ rows: 4, expandable: true, symbol: '看裁决原文' }}
        >
          {one.text}
        </Typography.Paragraph>
      ))}
    </Space>
  )
}
