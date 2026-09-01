/**
 * 会话面板：把会话逻辑（`useConversation`）与消息区、待选项、输入框摆在一起。
 *
 * 各领域通用——立项对焦、角色设定、以后的地图与场景都用它，换的只是 `agentCode` /
 * `targetKind` / `targetRef` 三个入参与几句文案：
 *
 * 1. 后端加好该领域的 agent 与 `target_kind`（会话按 target 一物一条，不用另写创建逻辑）
 * 2. 页面里 `<ChatPanel agentCode="…" targetKind="…" targetRef={id} who="…" starters={[…]} />`
 * 3. 要另一套排布就直接用 `useConversation` + `MessageList` + `Composer` 自己拼
 *
 * 两条通道要分开看：字一个个出现靠 SSE，但这一轮真正的结果来自 `POST /messages` 的返回与
 * 随后刷新的详情。所以流断了不算出事——增量只是让等待不难受，落库的那份才是真相。
 */
import { Alert, Card, Col, Row, Space, Spin, Tag, Typography } from 'antd'
import type { ReactNode } from 'react'

import ChoicePicker from '@/components/chat/ChoicePicker'
import Composer from '@/components/chat/Composer'
import DraftDiffPanel from '@/components/chat/DraftDiffPanel'
import MessageList from '@/components/chat/MessageList'
import { useConversation, type Handoff } from '@/components/chat/useConversation'
import type { TargetKind } from '@/types/api'

interface Props {
  projectCode: string
  agentCode: string
  targetKind: TargetKind
  targetRef?: string | null
  /** 新会话的标题，留空让后端按 agent 取一个。 */
  title?: string
  /** 报出当下看的是哪场会话：评审要拿它做「驳回后自动重生」。 */
  onActiveChange?: (id: string | null) => void
  /** 父页面递进来的下一轮说辞。 */
  handoff?: Handoff | null
  /** 标题那行字。会话由系统管，这里不摆任何会话入口。 */
  heading?: string
  /** 对面那位怎么称呼。每条消息自己带 `agent_code` 的话按它取，取不到才用这个。 */
  who?: string
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
  /** 用户还没开口时摆在输入框上面的示例说辞，点一下填进输入框。 */
  starters?: string[]
}

export type { Handoff }

export default function ChatPanel({
  projectCode,
  agentCode,
  targetKind,
  targetRef = null,
  title,
  onActiveChange,
  handoff = null,
  heading = '对焦',
  who = '设计师',
  draftsAside = false,
  sidebar = null,
  finale = null,
  finaleTitle = '收口',
  finaleKey = '',
  starters = [],
}: Props) {
  const talk = useConversation({
    projectCode,
    agentCode,
    targetKind,
    targetRef,
    title,
    handoff,
    onActiveChange,
  })
  const detail = talk.detail
  const conversation = detail?.conversation

  // 只在用户还没开口时摆开场短语：聊起来之后它就是干扰
  const opening =
    talk.input === '' && talk.pending === null && !talk.messages.some((one) => one.role === 'user')

  return (
    <Layout aside={draftsAside}>
      <Card
        size="small"
        title={
          <Typography.Text strong style={{ fontSize: 13 }}>
            {heading}
          </Typography.Text>
        }
        extra={
          talk.messages.length > 0 && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              共 {talk.messages.length} 条
            </Typography.Text>
          )
        }
      >
        {talk.failure !== '' ? (
          <Alert type="error" showIcon message="接不上这一场会话" description={talk.failure} />
        ) : talk.preparing ? (
          <Space size={6}>
            <Spin size="small" />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              正在接上这一场会话……
            </Typography.Text>
          </Space>
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
              {detail?.memory.summary && (
                <Alert
                  type="info"
                  message="前几轮的摘要"
                  description={
                    <Space direction="vertical" size={2} style={{ fontSize: 12 }}>
                      <span>{detail.memory.summary}</span>
                      {detail.memory.open_questions.length > 0 && (
                        <span>还没定：{detail.memory.open_questions.join('；')}</span>
                      )}
                    </Space>
                  }
                />
              )}
              <MessageList
                messages={talk.messages}
                pending={talk.pending}
                streaming={talk.streaming}
                interrupting={talk.interrupting}
                onInterrupt={talk.interrupt}
                loading={talk.detailLoading}
                briefing={detail?.briefing ?? ''}
                briefingBlank={detail?.briefing_blank ?? false}
                height={draftsAside ? 560 : 420}
                who={who}
              />
              <ChoicePicker
                groups={detail?.choices ?? []}
                disabled={talk.busy}
                onSubmit={talk.say}
                finaleTitle={finaleTitle}
                finaleKey={finaleKey}
                finale={
                  finale === null
                    ? null
                    : (close) =>
                        finale((text) => {
                          close()
                          talk.say(text)
                        })
                }
              />
              <Composer
                value={talk.input}
                onChange={talk.setInput}
                onSubmit={talk.submit}
                busy={talk.busy}
                who={who}
                starters={opening ? starters : []}
              />
            </Space>
          </div>
        )}
      </Card>
      {sidebar !== null ? (
        sidebar
      ) : (
        <DraftDiffPanel
          projectCode={projectCode}
          conversationId={talk.id ?? ''}
          drafts={detail?.drafts ?? []}
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
