/**
 * 消息区。
 *
 * 聊过的每一条都摆在这里，向上滚就能回看，折进摘要的那几条只是淡一档并标一句「已折进摘要」
 * ——它们没被删，只是不再进上下文。收起来会让人以为聊过的东西丢了。
 *
 * 气泡里只摆给人看的那几段话：结构块（草稿、待选项、命名建议、进度）各自在界面上已经有位置，
 * 原文再摆一遍就是同一件事看两遍。
 *
 * 一场会话里可以有多个 Agent 说话（主 Agent 指派子 Agent 生图、评审），所以称谓按每条消息
 * 自己的 `agent_code` 取，不按面板取；带出来的图挂在 `attachments` 上，跟着那条气泡走。
 */
import { Button, Image, Space, Spin, Typography } from 'antd'
import { useEffect, useRef } from 'react'

import MarkdownText from '@/components/MarkdownText'
import { visibleText } from '@/lib/message'
import type { Message } from '@/types/api'

/** 各 Agent 在气泡上的称谓。表里没有的用面板给的 `who` 兜底。 */
const WHO: Record<string, string> = {
  game_designer: '设计师',
  spec_writer: '设定作者',
  spec_reviewer: '评审',
  prompt_smith: '提示词师',
  image_t2i: '画师',
  image_i2i: '改图师',
  vision_reviewer: '视觉评审',
}

export default function MessageList({
  messages,
  pending,
  streaming,
  interrupting,
  onInterrupt,
  loading,
  briefing,
  briefingBlank,
  height,
  who,
}: {
  messages: Message[]
  /** 本轮刚发出去的那句：先摆成气泡，不等落库后才看见。 */
  pending: string | null
  streaming: string | null
  interrupting: boolean
  onInterrupt: () => void
  loading: boolean
  /** 开场提示：项目现状与接下来该说什么。后端现算，摆在历史消息前面。 */
  briefing: string
  /** 真则这只是一句号召，项目还没有任何东西可总结。 */
  briefingBlank: boolean
  /** 消息区高度：这一页越是以聊为主，就该给得越高。 */
  height: number
  /** 这一页对面那位怎么称呼，`WHO` 里查不到时用它。 */
  who: string
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
          if (one.status === 'thinking') {
            // 库里说这一轮在跑：字有多少摆多少，一个字都还没有就摆转圈
            return (
              <Thinking
                key={one.id}
                text={streaming ?? ''}
                who={nameOf(one, who)}
                interrupting={interrupting}
                onInterrupt={onInterrupt}
              />
            )
          }
          if (one.status === 'failed' || one.status === 'cancelled') {
            return <Aborted key={one.id} status={one.status} reason={one.content} />
          }
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
                <Shots items={one.attachments} />
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
        {streaming !== null && !messages.some((one) => one.status === 'thinking') && (
          <Thinking
            text={streaming}
            who={who}
            interrupting={interrupting}
            onInterrupt={onInterrupt}
          />
        )}
      </Space>
    </div>
  )
}

/** 这条消息署谁的名。 */
function nameOf(one: Message, fallback: string): string {
  return WHO[one.agent_code] ?? fallback
}

/**
 * 正在想的那一轮：转圈或已经出来的那几个字，底下挂一个中断。
 *
 * 中断这颗按钮跟着这条气泡走，不摆在输入框边上：卡住的是这一轮，用户要掐的也是这一轮。
 */
function Thinking({
  text,
  who,
  interrupting,
  onInterrupt,
}: {
  text: string
  who: string
  interrupting: boolean
  onInterrupt: () => void
}) {
  return (
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
        {text === '' ? (
          <Space size={6}>
            <Spin size="small" />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {who}正在思考中，请先等一下
            </Typography.Text>
          </Space>
        ) : (
          <Body text={text} />
        )}
        <div style={{ marginTop: 4 }}>
          <Button size="small" type="text" danger loading={interrupting} onClick={onInterrupt}>
            中断思考
          </Button>
        </div>
      </div>
    </div>
  )
}

/** 没走完的那一轮：炸了就把错因摆出来，被中断就说一句，都不当回答算。 */
function Aborted({ status, reason }: { status: string; reason: string }) {
  const failed = status === 'failed'
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-start' }}>
      <div
        style={{
          maxWidth: '86%',
          background: failed ? '#fff2f0' : '#fafafa',
          borderRadius: 8,
          padding: '6px 12px',
          wordBreak: 'break-word',
        }}
      >
        <Typography.Text type={failed ? 'danger' : 'secondary'} style={{ fontSize: 12 }}>
          {failed ? `这一轮没成：${reason}` : '这一轮被你中断了'}
        </Typography.Text>
      </div>
    </div>
  )
}

const BUBBLE: Record<string, { background: string; align: string }> = {
  user: { background: '#e6f4ff', align: 'flex-end' },
  assistant: { background: '#fafafa', align: 'flex-start' },
}

/**
 * 这条消息带出来的图。
 *
 * 子 Agent 生的图跟着那条消息走，用户对着它说「腿再长一点」就是下一轮的入参。后端现在还不会
 * 往 `attachments` 里塞东西（见 `agents/orchestrator.py`），所以这一段暂时不会出现。
 *
 * 接线时给每个附件带上 `url`（复用 `/api/characters/{id}/renders/{gid}/image` 那类取图
 * 口子）：渲染进程读不到磁盘，光有相对路径显不出图，只能当文件名摆着。
 */
function Shots({ items }: { items: Message['attachments'] }) {
  const shots = items.filter((one) => one.kind === 'image')
  if (shots.length === 0) return null
  return (
    <Space size={6} wrap style={{ marginTop: 6 }}>
      {shots.map((one, index) =>
        typeof one.url === 'string' ? (
          <Image
            key={one.url}
            src={one.url}
            width={96}
            height={96}
            style={{ objectFit: 'cover' }}
          />
        ) : (
          <Typography.Text key={index} type="secondary" style={{ fontSize: 12 }}>
            {String(one.path ?? '一张图')}
          </Typography.Text>
        ),
      )}
    </Space>
  )
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
