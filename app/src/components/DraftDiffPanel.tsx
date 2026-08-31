/**
 * 草稿 diff 与沉淀。
 *
 * 这一屏是「未确认时磁盘无改动」这条规矩的兑现处：Agent 写出来的东西全在库里当草稿躺着，
 * 用户在这里看清改了哪几行，按下沉淀才真正落盘（旧定稿退位进同级 `tmp/`）。
 *
 * `stale` 要提前示警而不是等 409：草稿是照某一份定稿改的，中间那份被别处改过，沉淀就会
 * 把别人的修改盖掉——后端为此直接拒收。与其让用户点完才看见报错，不如按钮就先禁掉。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Popconfirm,
  Segmented,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd'
import { useEffect, useState } from 'react'

import { commitConversation, discardConversation, readDiff } from '@/api/conversations'
import { collapseUnchanged, diffLines, diffStat } from '@/lib/diff'
import type { Draft } from '@/types/api'

interface Props {
  conversationId: string
  drafts: Draft[]
  /** 挤在窄栏里：按钮改成竖排块级，diff 只留一小段能滚。 */
  compact?: boolean
}

const MONO = 'ui-monospace, SFMono-Regular, Menlo, monospace'

export default function DraftDiffPanel({ conversationId, drafts, compact = false }: Props) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [active, setActive] = useState<string | null>(null)
  const [picked, setPicked] = useState<string[] | null>(null)

  useEffect(() => {
    // 草稿换了一批（新一轮生成完）就重新按当前列表取默认值，别停在已经不存在的 id 上
    setActive(null)
    setPicked(null)
  }, [drafts])

  const current = drafts.find((draft) => draft.id === active) ?? drafts[0]
  // 过期的那几份默认不勾：勾上等于让整次沉淀被拒
  const selected = picked ?? drafts.filter((draft) => !draft.stale).map((draft) => draft.id)

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['conversation', conversationId] })
    void queryClient.invalidateQueries({ queryKey: ['art-bible'] })
  }

  const commit = useMutation({
    mutationFn: () =>
      commitConversation(conversationId, selected.length === drafts.length ? undefined : selected),
    onSuccess: (result) => {
      const moved = result.archived.filter((one) => one.previous_path !== null).length
      message.success(
        moved > 0
          ? `已落盘 ${result.archived.length} 份，${moved} 份旧定稿退位进 tmp/`
          : `已落盘 ${result.archived.length} 份`,
      )
      refresh()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const discard = useMutation({
    mutationFn: () => discardConversation(conversationId),
    onSuccess: (result) => {
      message.success(`丢掉了 ${result.discarded} 份草稿，磁盘没动过`)
      refresh()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const canCommit = selected.length > 0 && !drafts.some((d) => d.stale && selected.includes(d.id))

  const discardBtn = (
    <Popconfirm
      title="丢掉全部草稿？"
      description="库里的草稿会标成弃用，磁盘上的定稿保持原样。"
      okText="丢掉"
      cancelText="算了"
      onConfirm={() => discard.mutate()}
    >
      <Button danger block={compact} loading={discard.isPending}>
        丢弃草稿
      </Button>
    </Popconfirm>
  )
  const commitBtn = (
    <Popconfirm
      title={`把 ${selected.length} 份写进项目目录？`}
      description="现有定稿会先挪进同级 tmp/ 留底，再写入新版本。"
      okText="沉淀"
      cancelText="再看看"
      onConfirm={() => commit.mutate()}
    >
      <Button type="primary" block={compact} disabled={!canCommit} loading={commit.isPending}>
        确认沉淀（{selected.length}）
      </Button>
    </Popconfirm>
  )
  const actions = compact ? (
    <Space direction="vertical" size={6} style={{ width: '100%' }}>
      {commitBtn}
      {discardBtn}
    </Space>
  ) : (
    <Space>
      {discardBtn}
      {commitBtn}
    </Space>
  )

  return (
    <Card size="small" title="待办" extra={drafts.length > 0 && !compact ? actions : undefined}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {drafts.length === 0 ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            Agent 写出的定稿会先在这里等你过目，确认之前磁盘一个字节都不会动。
          </Typography.Text>
        ) : (
          <>
            {drafts.length > 1 && (
              <Segmented
                value={current?.id}
                onChange={(value) => setActive(String(value))}
                options={drafts.map((draft) => ({
                  value: draft.id,
                  label: draft.target_path.split('/').pop() ?? draft.target_path,
                }))}
              />
            )}
            <Checkbox.Group
              value={selected}
              onChange={(value) => setPicked(value as string[])}
              style={{
                display: 'flex',
                flexDirection: 'column',
                gap: 4,
                wordBreak: 'break-all',
              }}
              options={drafts.map((draft) => ({
                value: draft.id,
                label: draft.stale
                  ? `${draft.target_path}（基线已变，沉淀会被拒）`
                  : draft.target_path,
                disabled: draft.stale,
              }))}
            />
            {current && (
              <DiffView conversationId={conversationId} draft={current} compact={compact} />
            )}
            {compact && actions}
          </>
        )}
      </Space>
    </Card>
  )
}

function DiffView({
  conversationId,
  draft,
  compact,
}: {
  conversationId: string
  draft: Draft
  /** 窄栏里只留一小扇窗，并去掉行号：两道行号能吃掉大半个宽度。 */
  compact: boolean
}) {
  const [opened, setOpened] = useState<number[]>([])
  const diff = useQuery({
    queryKey: ['draft-diff', conversationId, draft.id],
    queryFn: () => readDiff(conversationId, draft.id),
  })

  useEffect(() => {
    setOpened([])
  }, [draft.id])

  if (diff.isLoading) return <Spin />
  if (diff.error) return <Alert type="error" showIcon message={diff.error.message} />
  if (!diff.data) return null

  const lines = diffLines(diff.data.current, diff.data.draft)
  const stat = diffStat(lines)
  const chunks = collapseUnchanged(lines)

  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Space size={8}>
        <Typography.Text code>{diff.data.target_path}</Typography.Text>
        <Tag color="green">+{stat.added}</Tag>
        <Tag color="red">-{stat.removed}</Tag>
        {stat.identical && <Tag>与现有定稿一字不差</Tag>}
      </Space>
      {diff.data.stale && (
        <Alert
          type="warning"
          showIcon
          message="这份草稿的基线已经过期"
          description="它是照旧版定稿改的，之间那份被别处改过。直接沉淀会盖掉别人的修改，所以后端会拒收——让 Agent 照当前定稿再拟一版。"
        />
      )}
      {diff.data.warnings.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message="沉下去会留这几处空"
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {diff.data.warnings.map((one) => (
                <li key={one}>{one}</li>
              ))}
            </ul>
          }
        />
      )}
      <div
        style={{
          border: '1px solid #f0f0f0',
          borderRadius: 6,
          maxHeight: compact ? 240 : 420,
          overflow: 'auto',
          fontFamily: MONO,
          fontSize: 12.5,
          lineHeight: '20px',
        }}
      >
        {chunks.map((chunk, index) =>
          chunk.kind === 'gap' && !opened.includes(index) ? (
            <div
              key={index}
              onClick={() => setOpened((prev) => [...prev, index])}
              style={{
                padding: '2px 12px',
                background: '#fafafa',
                color: '#8c8c8c',
                cursor: 'pointer',
                borderTop: '1px solid #f0f0f0',
                borderBottom: '1px solid #f0f0f0',
              }}
            >
              ⋯ 未改动的 {chunk.lines.length} 行，点开看
            </div>
          ) : (
            chunk.lines.map((line, seq) => (
              <DiffRow key={`${index}-${seq}`} line={line} compact={compact} />
            ))
          ),
        )}
      </div>
    </Space>
  )
}

const TINT: Record<string, string> = {
  added: '#f6ffed',
  removed: '#fff1f0',
  same: 'transparent',
}
const SIGN: Record<string, string> = { added: '+', removed: '-', same: ' ' }

function DiffRow({
  line,
  compact,
}: {
  line: ReturnType<typeof diffLines>[number]
  compact: boolean
}) {
  return (
    <div style={{ display: 'flex', background: TINT[line.kind], whiteSpace: 'pre-wrap' }}>
      {!compact && (
        <>
          <span style={{ width: 44, textAlign: 'right', paddingRight: 8, color: '#bfbfbf' }}>
            {line.currentNo ?? ''}
          </span>
          <span style={{ width: 44, textAlign: 'right', paddingRight: 8, color: '#bfbfbf' }}>
            {line.draftNo ?? ''}
          </span>
        </>
      )}
      <span style={{ width: 16, color: '#8c8c8c' }}>{SIGN[line.kind]}</span>
      <span style={{ flex: 1, paddingRight: 12, wordBreak: 'break-word' }}>{line.text || ' '}</span>
    </div>
  )
}
