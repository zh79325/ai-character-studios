/**
 * 会话面板。
 *
 * 两条通道要分开看：字一个个出现靠 SSE，但这一轮真正的结果来自 `POST /messages` 的返回与
 * 随后刷新的详情。所以流断了不算出事——增量只是让等待不难受，落库的那份才是真相。
 *
 * 顺序是先发消息再订流：后端开工第一步会清掉上一轮的增量缓冲，订流赶在它前面就会把上一轮
 * 重放一遍，而那段末尾的 `turn` 一到流就收了，这一轮反而一个字也看不见。漏掉开头几段不用担
 * 心：缓冲里就剩这一轮，订上来从头补。
 *
 * 聊过的每一条都摆在消息区里，向上滚就能回看，折进摘要的那几条只是淡一档并标一句「已折进摘要」
 * ——它们没被删，只是不再进上下文。收起来会让人以为聊过的东西丢了。
 *
 * 气泡里只摆给人看的那几段话：结构块（草稿、待选项、命名建议、进度）各自在界面上已经有位置，
 * 原文再摆一遍就是同一件事看两遍。
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
  Tag,
  Typography,
} from 'antd'
import { useEffect, useRef, useState, type ReactNode } from 'react'

import {
  ensureConversation,
  listConversations,
  readConversation,
  sendMessage,
  startConversation,
  subscribeConversation,
} from '@/api/conversations'
import ChoicePicker from '@/components/ChoicePicker'
import DraftDiffPanel from '@/components/DraftDiffPanel'
import MarkdownText from '@/components/MarkdownText'
import { visibleText } from '@/lib/message'
import type { Conversation, ConversationDetail, Message, TargetKind } from '@/types/api'

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
   * 会话由系统管起来：进面板就接上该聊的那场，面板上不摆任何会话入口。
   *
   * 立项对焦就一条线，既不需要用户先做一次开会话的动作，也没有第二场可选。
   */
  managed?: boolean
  /**
   * 草稿挤到边上的窄栏，对话占满剩下的宽度。
   *
   * 立项对焦这一页的主体就是聊，草稿只是聊出来的副产物，摆得跟对话一样大只会抢位置。
   */
  draftsAside?: boolean
  /** 边上那条窄栏整块换掉（立项页只留快捷导航）：草稿区不是每一页都用得上。 */
  sidebar?: ReactNode | null
  /**
   * 摆在待选项抽屉最后的收口动作（立项页：确认游戏风格、确认立项）。
   *
   * 收口跟这一轮的题摆在一处：拍完几项接着就能收口，也可以关掉抽屉继续聊。它拿到的 `say`
   * 会顺手关掉抽屉再发话。
   */
  finale?: ((say: (text: string) => void) => ReactNode) | null
  /** 收口这一步叫什么，用在抽屉标题与重开按钮上。 */
  finaleTitle?: string
  /** 收口内容的签名：换了抽屉自己弹出来，跟新一批待选项一模一样。空串就是没收口可做。 */
  finaleKey?: string
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
  draftsAside = false,
  sidebar = null,
  finale = null,
  finaleTitle = '收口',
  finaleKey = '',
}: Props) {
  const { message: toast } = App.useApp()
  const queryClient = useQueryClient()
  const [active, setActive] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState<string | null>(null)
  /** 已经发出去、还没回到会话详情里的那句话。 */
  const [pending, setPending] = useState<string | null>(null)
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
      // 先发出去（不等），后端清掉上一轮缓冲之后这条流才订得上本轮的字
      const turn = sendMessage(chosen, content)
      stop.current = subscribeConversation({
        conversationId: chosen,
        onDelta: (piece) => setStreaming((prev) => (prev ?? '') + piece),
        // 失败的措辞由发消息那头统一报，这里再弹一次就是同一件事说两遍
        onError: () => setStreaming(null),
      })
      try {
        return await turn
      } finally {
        stop.current?.()
        stop.current = null
        setStreaming(null)
      }
    },
    onSuccess: async (turn) => {
      // 等详情真拉回来再抖掉那句话，不然气泡会先消失一下再出现
      await queryClient.invalidateQueries({ queryKey: ['conversation', chosen] })
      setPending(null)
      void queryClient.invalidateQueries({ queryKey: ['conversations'] })
      if (turn.folded_turns.length > 0) {
        toast.info(`上下文快满了，第 ${turn.folded_turns.join('、')} 轮已折进摘要，原文还在`)
      }
    },
    // 后端是先落库再调模型，所以要先看这句话到底存下没：存下了就别再送回输入框，否则重发会冒出两条同样的话
    onError: async (err: Error, content) => {
      await queryClient.invalidateQueries({ queryKey: ['conversation', chosen] })
      const fresh = queryClient.getQueryData<ConversationDetail>(['conversation', chosen])
      const landed = fresh?.messages.some((one) => one.role === 'user' && one.content === content)
      setPending(null)
      if (landed !== true) setInput((prev) => (prev === '' ? content : prev))
      toast.error(err.message)
    },
  })

  const conversation = detail.data?.conversation
  const messages = (detail.data?.messages ?? []).filter((one) => one.role !== 'system')
  const preparing = managed && chosen === null

  // 只认 nonce：同一句话递两次是两件事，而重渲染不是
  const handled = useRef(0)
  useEffect(() => {
    if (handoff === null || handoff.nonce === handled.current) return
    handled.current = handoff.nonce
    setInput(handoff.text)
    if (handoff.fresh) open.mutate()
  }, [handoff, open])

  /** 发一句话出去。选择组件拼出来的那句也走这里，跟手打的一样进气泡。 */
  const dispatch = (content: string) => {
    if (send.isPending) return
    // 先清输入框、先把话摆上去：点完发送还看见自己那段字蹲在输入框里，像没发出去
    setInput('')
    setPending(content)
    send.mutate(content)
  }

  const submit = () => {
    const content = input.trim()
    if (content) dispatch(content)
  }

  return (
    <Layout aside={draftsAside}>
      <Card
        size="small"
        title={
          managed ? (
            <Typography.Text strong style={{ fontSize: 13 }}>
              对焦
            </Typography.Text>
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
          messages.length > 0 && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              共 {messages.length} 条
            </Typography.Text>
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
          // 待选项抽屉贴着这一层往上抽，盖住输入框与消息区；relative 是它的定位锚
          <div style={{ position: 'relative', overflow: 'hidden' }}>
            <Space direction="vertical" size={10} style={{ width: '100%' }}>
              {conversation && (
                <Space size={6} wrap>
                  <Tag color="blue">{conversation.bound_provider_label}</Tag>
                  {conversation.rebind_count > 0 && (
                    <Tag color="orange" title={conversation.rebind_reason ?? undefined}>
                      换过 {conversation.rebind_count} 次服务商
                    </Tag>
                  )}
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
                messages={messages}
                pending={pending}
                streaming={streaming}
                loading={detail.isLoading}
                briefing={detail.data?.briefing ?? ''}
                briefingBlank={detail.data?.briefing_blank ?? false}
                height={draftsAside ? 560 : 420}
              />
              <ChoicePicker
                groups={detail.data?.choices ?? []}
                disabled={send.isPending}
                onSubmit={dispatch}
                finaleTitle={finaleTitle}
                finaleKey={finaleKey}
                finale={
                  finale === null
                    ? null
                    : (close) =>
                        finale((text) => {
                          close()
                          dispatch(text)
                        })
                }
              />
              <Input.TextArea
                value={input}
                rows={3}
                disabled={send.isPending}
                placeholder={
                  send.isPending
                    ? '等设计师回这一轮，回完再接着说'
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
                disabled={send.isPending}
                onClick={submit}
              >
                {send.isPending ? '等回话' : '发送'}
              </Button>
            </Space>
          </div>
        )}
      </Card>
      {sidebar !== null ? (
        sidebar
      ) : chosen === null ? (
        <Card size="small" title="待办">
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            会话产出的定稿会在这里等你过目。
          </Typography.Text>
        </Card>
      ) : (
        <DraftDiffPanel
          conversationId={chosen}
          drafts={detail.data?.drafts ?? []}
          compact={draftsAside}
        />
      )}
    </Layout>
  )
}

/**
 * 对话与草稿的排布。
 *
 * `aside` 真时草稿收成一条固定窄栏，对话吃掉剩下的宽度；假时两边各占一半，草稿里的 diff
 * 才有地方铺开。
 */
function Layout({ aside, children }: { aside: boolean; children: [ReactNode, ReactNode] }) {
  const [chat, drafts] = children

  if (aside) {
    return (
      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 0 }}>{chat}</div>
        <div style={{ width: 300, flex: '0 0 300px' }}>{drafts}</div>
      </div>
    )
  }

  return (
    <Row gutter={16}>
      <Col span={13}>{chat}</Col>
      <Col span={11}>{drafts}</Col>
    </Row>
  )
}

/** 一场会话在下拉里的样子。 */
function conversationLabel(one: Conversation): string {
  return `${one.title}（${one.message_count} 条）`
}

const BUBBLE: Record<string, { background: string; align: string }> = {
  user: { background: '#e6f4ff', align: 'flex-end' },
  assistant: { background: '#fafafa', align: 'flex-start' },
}

/**
 * 一条助手消息的正文。
 *
 * 结构块已经在草稿区、待选项抽屉与立项收口上各自摆过了，这里只给剩下的话。整轮全是块的时候
 * 报一句去哪里看，而不是留一个空气泡。
 */
function Body({ text }: { text: string }) {
  const body = visibleText(text)
  if (body === '') {
    return (
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        这一轮的东西都在草稿与待选项里
      </Typography.Text>
    )
  }
  return <MarkdownText text={body} />
}

function MessageList({
  messages,
  pending,
  streaming,
  loading,
  briefing,
  briefingBlank,
  height,
}: {
  messages: Message[]
  /** 本轮刚发出去的那句：先摆成气泡，不等落库后才看见。 */
  pending: string | null
  streaming: string | null
  loading: boolean
  /** 开场提示：项目现状与接下来该说什么。后端现算，摆在历史消息前面。 */
  briefing: string
  /** 真则这只是一句号召，项目还没有任何东西可总结。 */
  briefingBlank: boolean
  /** 消息区高度：这一页越是以聊为主，就该给得越高。 */
  height: number
}) {
  const box = useRef<HTMLDivElement>(null)
  /** 看的是不是最新那几条。用户往上翻着历史时不能再把他拽回底部。 */
  const glued = useRef(true)
  // 计数与末条 id 一起认：消息数组每次重渲染都是新的，按引用盯会变成每渲染一次强制置底
  const tail = `${messages.length}:${messages.at(-1)?.id ?? ''}`

  useEffect(() => {
    // 新内容一到就贴到底部，不然生成中的字会长到看不见的地方去
    const node = box.current
    if (node && glued.current) node.scrollTop = node.scrollHeight
  }, [tail, pending, streaming])

  // 一句号召摆成气泡，看着像 AI 已经先开口说过话了；铺成居中大字才是「这一屏在等你说」
  const hero = briefingBlank && messages.length === 0 && pending === null && streaming === null
  if (hero) {
    return (
      <div
        style={{
          height,
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
    <div
      ref={box}
      style={{ height, overflowY: 'auto', padding: '4px 2px' }}
      onScroll={(event) => {
        // 离底不到一屏的一小段算还在看最新：精确到像素的话流式输出自己就会把自己顶出去
        const node = event.currentTarget
        glued.current = node.scrollHeight - node.scrollTop - node.clientHeight < 48
      }}
    >
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
                  // 用户那边是纯文本，换行得自己留；Agent 那边交给 Markdown 排
                  whiteSpace: one.role === 'user' ? 'pre-wrap' : undefined,
                  wordBreak: 'break-word',
                  fontSize: 13,
                }}
              >
                {one.folded && (
                  <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                    已折进摘要 ·{' '}
                  </Typography.Text>
                )}
                {one.role === 'user' ? one.content : <Body text={one.content} />}
              </div>
            </div>
          )
        })}
        {pending !== null && (
          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <div
              style={{
                maxWidth: '86%',
                background: BUBBLE.user!.background,
                borderRadius: 8,
                padding: '8px 12px',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                fontSize: 13,
              }}
            >
              {pending}
            </div>
          </div>
        )}
        {streaming !== null && (
          <div style={{ display: 'flex', justifyContent: 'flex-start' }}>
            <div
              style={{
                maxWidth: '86%',
                background: '#fafafa',
                borderRadius: 8,
                padding: '8px 12px',
                wordBreak: 'break-word',
                fontSize: 13,
              }}
            >
              {streaming === '' ? (
                <Space size={6}>
                  <Spin size="small" />
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    设计师在想，头几个字出来前先等一下
                  </Typography.Text>
                </Space>
              ) : (
                <Body text={streaming} />
              )}
            </div>
          </div>
        )}
      </Space>
    </div>
  )
}
