/**
 * 会话面板。
 *
 * 两条通道要分开看：字一个个出现靠 SSE，但这一轮真正的结果来自 `POST /messages` 的返回与
 * 随后刷新的详情。所以流断了不算出事——增量只是让等待不难受，落库的那份才是真相。
 *
 * 顺序是先订流再发消息：反过来的话第一批增量已经推完了，缓冲里的那段虽然还能补上，但会一
 * 次性砸出来，看着像卡了几秒。
 *
 * 折进摘要的消息默认收起。它们没被删，只是不再进上下文——不给用户看见「原文还在」，会让人
 * 以为聊过的东西丢了。
 */
import { PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Empty,
  Input,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from 'antd'
import { useEffect, useRef, useState } from 'react'

import {
  ensureConversation,
  listConversations,
  readConversation,
  sendMessage,
  startConversation,
  subscribeConversation,
} from '@/api/conversations'
import DraftDiffPanel from '@/components/DraftDiffPanel'
import type { Conversation, Message, TargetKind } from '@/types/api'

interface Props {
  agentCode: string
  targetKind: TargetKind
  targetRef?: string | null
  /** 新会话的标题，留空让后端按 agent 取一个。 */
  title?: string
  /** 报出当下看的是哪场会话：评审要拿它做「驳回后自动重生」。 */
  onActiveChange?: (id: string | null) => void
  /** 父页面递进来的下一轮说辞。 */
  handoff?: Handoff | null
  /**
   * 会话由系统管起来：进面板就接上该聊的那场，用户不用点「新会话」。
   *
   * 立项对焦本来只有一条线，让用户先做一次开会话的动作纯属多余。历史会话仍然能切过去看，
   * 但那是回看，不是管理。
   */
  managed?: boolean
}

/**
 * 从外面递进来的一句话。
 *
 * 只预填不自动发：这句话是平台替用户拟的，直接发出去等于拿用户的名义说了一句他没看过的话。
 */
export interface Handoff {
  text: string
  /** 真则先开一场新会话：换方向时旧上下文会把模型拉回原方向。 */
  fresh: boolean
  /** 同一句话可能递两次（用户又按了一遍），用它区分。 */
  nonce: number
}

export default function ChatPanel({
  agentCode,
  targetKind,
  targetRef = null,
  title,
  onActiveChange,
  handoff = null,
  managed = false,
}: Props) {
  const { message: toast } = App.useApp()
  const queryClient = useQueryClient()
  const [active, setActive] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState<string | null>(null)
  const [showFolded, setShowFolded] = useState(false)
  const stop = useRef<(() => void) | null>(null)

  const list = useQuery({
    queryKey: ['conversations', targetKind, targetRef, agentCode],
    queryFn: () => listConversations({ targetKind, targetRef: targetRef ?? undefined }),
  })
  const mine = (list.data ?? []).filter((one) => one.agent_code === agentCode)

  // 托管模式下这一口负责「进来就有会话」：它是幂等的，上一场还开着就是原来那场
  const ensured = useQuery({
    queryKey: ['conversation-ensure', targetKind, targetRef, agentCode],
    queryFn: () =>
      ensureConversation({
        agent_code: agentCode,
        target_kind: targetKind,
        target_ref: targetRef,
        title,
      }),
    enabled: managed,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  })

  useEffect(() => {
    const fresh = ensured.data
    if (fresh === undefined) return
    queryClient.setQueryData(['conversation', fresh.conversation.id], fresh)
    void queryClient.invalidateQueries({ queryKey: ['conversations'] })
  }, [ensured.data, queryClient])

  // 默认接着最近那场聊：一次没聊完的会话比一张空白面板有用
  const chosen = active ?? ensured.data?.conversation.id ?? mine[0]?.id ?? null

  const detail = useQuery({
    queryKey: ['conversation', chosen],
    queryFn: () => readConversation(chosen!),
    enabled: chosen !== null,
  })

  // 离开面板要退订，否则这条连接会一直挂在后端的事件循环上
  useEffect(() => () => stop.current?.(), [])

  useEffect(() => {
    onActiveChange?.(chosen)
  }, [chosen, onActiveChange])

  const open = useMutation({
    mutationFn: () =>
      startConversation({
        agent_code: agentCode,
        target_kind: targetKind,
        target_ref: targetRef,
        title,
      }),
    onSuccess: (fresh) => {
      queryClient.setQueryData(['conversation', fresh.conversation.id], fresh)
      void queryClient.invalidateQueries({ queryKey: ['conversations'] })
      setActive(fresh.conversation.id)
    },
    onError: (err: Error) => toast.error(err.message),
  })

  const send = useMutation({
    mutationFn: async (content: string) => {
      if (!chosen) throw new Error('还没有会话')
      setStreaming('')
      stop.current?.()
      stop.current = subscribeConversation({
        conversationId: chosen,
        onDelta: (piece) => setStreaming((prev) => (prev ?? '') + piece),
        // 失败的措辞由发消息那头统一报，这里再弹一次就是同一件事说两遍
        onError: () => setStreaming(null),
      })
      try {
        return await sendMessage(chosen, content)
      } finally {
        stop.current?.()
        stop.current = null
        setStreaming(null)
      }
    },
    onSuccess: (turn) => {
      setInput('')
      void queryClient.invalidateQueries({ queryKey: ['conversation', chosen] })
      void queryClient.invalidateQueries({ queryKey: ['conversations'] })
      if (turn.folded_turns.length > 0) {
        toast.info(`上下文快满了，第 ${turn.folded_turns.join('、')} 轮已折进摘要，原文还在`)
      }
    },
    onError: (err: Error) => toast.error(err.message),
  })

  const conversation = detail.data?.conversation
  const frozen = conversation !== undefined && conversation.status !== 'active'
  const messages = (detail.data?.messages ?? []).filter((one) => one.role !== 'system')
  const foldedCount = messages.filter((one) => one.folded).length
  const shown = showFolded ? messages : messages.filter((one) => !one.folded)
  const preparing = managed && chosen === null

  // 只认 nonce：同一句话递两次是两件事，而重渲染不是
  const handled = useRef(0)
  useEffect(() => {
    if (handoff === null || handoff.nonce === handled.current) return
    handled.current = handoff.nonce
    setInput(handoff.text)
    if (handoff.fresh) open.mutate()
  }, [handoff, open])

  const submit = () => {
    const content = input.trim()
    if (!content || send.isPending) return
    send.mutate(content)
  }

  return (
    <Row gutter={16}>
      <Col span={13}>
        <Card
          size="small"
          title={
            managed ? (
              <Space size={8}>
                <Typography.Text strong style={{ fontSize: 13 }}>
                  对焦
                </Typography.Text>
                {mine.length > 1 && (
                  <Select
                    size="small"
                    style={{ minWidth: 220 }}
                    placeholder="历史会话"
                    value={chosen ?? undefined}
                    loading={list.isLoading}
                    onChange={setActive}
                    options={mine.map((one) => ({
                      value: one.id,
                      label: conversationLabel(one),
                    }))}
                  />
                )}
              </Space>
            ) : (
              <Space>
                <Select
                  size="small"
                  style={{ minWidth: 220 }}
                  placeholder="还没有会话"
                  value={chosen ?? undefined}
                  loading={list.isLoading}
                  onChange={setActive}
                  options={mine.map((one) => ({
                    value: one.id,
                    label: conversationLabel(one),
                  }))}
                />
                <Button
                  size="small"
                  icon={<PlusOutlined />}
                  loading={open.isPending}
                  onClick={() => open.mutate()}
                >
                  新会话
                </Button>
              </Space>
            )
          }
          extra={
            foldedCount > 0 && (
              <Space size={6}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  已折起 {foldedCount} 条
                </Typography.Text>
                <Switch size="small" checked={showFolded} onChange={setShowFolded} />
              </Space>
            )
          }
        >
          {chosen === null ? (
            preparing ? (
              <Empty image={null} description="正在接上这个项目的对焦会话……">
                <Spin />
              </Empty>
            ) : (
              <Empty
                image={null}
                description={`开一场会话，让 ${agentCode} 陪你把这份定稿聊清楚。它写出来的内容会先当草稿，你确认了才落盘。`}
              >
                <Button type="primary" loading={open.isPending} onClick={() => open.mutate()}>
                  开始
                </Button>
              </Empty>
            )
          ) : (
            <Space direction="vertical" size={10} style={{ width: '100%' }}>
              {conversation && (
                <Space size={6} wrap>
                  <Tag color="blue">{conversation.bound_provider_label}</Tag>
                  {conversation.rebind_count > 0 && (
                    <Tag color="orange" title={conversation.rebind_reason ?? undefined}>
                      换过 {conversation.rebind_count} 次服务商
                    </Tag>
                  )}
                  {frozen && <Tag>只读</Tag>}
                </Space>
              )}
              {detail.data?.memory.summary && (
                <Alert
                  type="info"
                  message="前几轮的摘要"
                  description={
                    <Space direction="vertical" size={2} style={{ fontSize: 12 }}>
                      <span>{detail.data.memory.summary}</span>
                      {detail.data.memory.open_questions.length > 0 && (
                        <span>还没定：{detail.data.memory.open_questions.join('；')}</span>
                      )}
                    </Space>
                  }
                />
              )}
              <MessageList
                messages={shown}
                streaming={streaming}
                loading={detail.isLoading}
                briefing={detail.data?.briefing ?? ''}
                briefingBlank={detail.data?.briefing_blank ?? false}
              />
              <Input.TextArea
                value={input}
                rows={3}
                disabled={frozen}
                placeholder={
                  frozen
                    ? '这场会话已经收尾了，开一场新的接着聊'
                    : '说清你要什么。Enter 发送，Shift+Enter 换行'
                }
                onChange={(event) => setInput(event.target.value)}
                onPressEnter={(event) => {
                  if (event.shiftKey) return
                  event.preventDefault()
                  submit()
                }}
              />
              <Button
                type="primary"
                block
                loading={send.isPending}
                disabled={frozen}
                onClick={submit}
              >
                发送
              </Button>
            </Space>
          )}
        </Card>
      </Col>
      <Col span={11}>
        {chosen === null ? (
          <Card size="small" title="待确认的改动">
            <Empty image={null} description="会话产出的定稿会在这里等你过目。" />
          </Card>
        ) : (
          <DraftDiffPanel
            conversationId={chosen}
            drafts={detail.data?.drafts ?? []}
            frozen={frozen}
          />
        )}
      </Col>
    </Row>
  )
}

/** 一场会话在下拉里的样子。 */
function conversationLabel(one: Conversation): string {
  const ended = one.status === 'committed' ? '，已沉淀' : '，已丢弃'
  return `${one.title}（${one.message_count} 条${one.status === 'active' ? '' : ended}）`
}

const BUBBLE: Record<string, { background: string; align: string }> = {
  user: { background: '#e6f4ff', align: 'flex-end' },
  assistant: { background: '#fafafa', align: 'flex-start' },
}

function MessageList({
  messages,
  streaming,
  loading,
  briefing,
  briefingBlank,
}: {
  messages: Message[]
  streaming: string | null
  loading: boolean
  /** 开场提示：项目现状与接下来该说什么。后端现算，摆在历史消息前面。 */
  briefing: string
  /** 真则这只是一句号召，项目还没有任何东西可总结。 */
  briefingBlank: boolean
}) {
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // 新内容一到就贴到底部，不然生成中的字会长到看不见的地方去
    const node = box.current
    if (node) node.scrollTop = node.scrollHeight
  }, [messages, streaming])

  // 一句号召摆成气泡，看着像 AI 已经先开口说过话了；铺成居中大字才是「这一屏在等你说」
  const hero = briefingBlank && messages.length === 0 && streaming === null
  if (hero) {
    return (
      <div
        style={{
          height: 420,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: '0 24px',
        }}
      >
        <Typography.Title level={3} style={{ margin: 0, textAlign: 'center', fontWeight: 600 }}>
          {briefing}
        </Typography.Title>
      </div>
    )
  }

  return (
    <div ref={box} style={{ height: 420, overflowY: 'auto', padding: '4px 2px' }}>
      {loading && <Spin />}
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        {briefing !== '' && !briefingBlank && (
          <div style={{ display: 'flex', justifyContent: 'flex-start' }}>
            <div
              style={{
                maxWidth: '86%',
                background: '#f6ffed',
                border: '1px solid #d9f7be',
                borderRadius: 8,
                padding: '8px 12px',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                fontSize: 13,
              }}
            >
              {briefing}
            </div>
          </div>
        )}
        {messages.map((one) => {
          const style = BUBBLE[one.role] ?? BUBBLE.assistant!
          return (
            <div key={one.id} style={{ display: 'flex', justifyContent: style.align }}>
              <div
                style={{
                  maxWidth: '86%',
                  background: style.background,
                  opacity: one.folded ? 0.55 : 1,
                  borderRadius: 8,
                  padding: '8px 12px',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  fontSize: 13,
                }}
              >
                {one.folded && (
                  <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                    已折进摘要 ·{' '}
                  </Typography.Text>
                )}
                {one.content}
              </div>
            </div>
          )
        })}
        {streaming !== null && (
          <div style={{ display: 'flex', justifyContent: 'flex-start' }}>
            <div
              style={{
                maxWidth: '86%',
                background: '#fafafa',
                borderRadius: 8,
                padding: '8px 12px',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                fontSize: 13,
              }}
            >
              {streaming === '' ? <Spin size="small" /> : streaming}
            </div>
          </div>
        )}
      </Space>
    </div>
  )
}
