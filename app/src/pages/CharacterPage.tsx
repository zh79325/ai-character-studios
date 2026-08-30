/**
 * 一个角色的工作台。
 *
 * 页面按「先聊出来、再审、再拍板」这一条线排：设定会话在上，评审与门禁在下，右侧是这个角色
 * 身上发生过的事。三块都盯着同一个 `character` 查询，所以门禁一按下去，状态、约束清单、时间
 * 线会一起变——分开各拿一份的话，用户会看到「已确认」旁边还挂着旧的待办按钮。
 *
 * 「改某一项重生」「换方向重生」不在这里直接发消息：卡片只把拟好的话递上来，用户在会话里过目
 * 后自己发。平台替用户开口，说错了却算在用户头上。
 *
 * 渲染图那张卡片要等门禁 1 过了才出现：设定是它的底本，没定稿就没有可翻译的东西，提前摆上
 * 按钮只会让用户按一下拿到 409。
 */
import { ArrowLeftOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Card, Col, Empty, Row, Space, Tag, Timeline, Typography } from 'antd'
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { listCharacterEvents, readCharacter } from '@/api/characters'
import { ApiError } from '@/api/client'
import ChatPanel, { type Handoff } from '@/components/ChatPanel'
import RenderGateCard from '@/components/RenderGateCard'
import SpecGateCard from '@/components/SpecGateCard'
import type { TaskEvent } from '@/types/api'

const WRITER = 'spec_writer'

/** 事件级别 → 时间线颜色。`warning` 是「审出问题」，不是出错。 */
const LEVEL_COLORS: Record<string, string> = {
  info: 'blue',
  warning: 'orange',
  error: 'red',
}

export default function CharacterPage() {
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [handoff, setHandoff] = useState<Handoff | null>(null)

  const character = useQuery({
    queryKey: ['character', id],
    queryFn: () => readCharacter(id),
    enabled: id !== '',
  })
  const events = useQuery({
    queryKey: ['character-events', id],
    queryFn: () => listCharacterEvents(id),
    enabled: id !== '',
  })

  if (character.error instanceof ApiError && character.error.status === 404) {
    return (
      <Card>
        <Empty description="这个角色不在当前项目里。可能是切过项目，或者它已经被移出。">
          <Typography.Link onClick={() => navigate('/project')}>回到当前项目</Typography.Link>
        </Empty>
      </Card>
    )
  }

  const row = character.data

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card size="small" loading={character.isLoading}>
        <Space direction="vertical" size={2}>
          <Space size={8}>
            <Typography.Link onClick={() => navigate('/project')}>
              <ArrowLeftOutlined /> 人物素材
            </Typography.Link>
            <Typography.Title level={5} style={{ margin: 0 }}>
              {row?.name ?? '…'}
            </Typography.Title>
            {row && <Tag color="blue">{row.state_label}</Tag>}
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {row?.dir_name ?? ''}
            {row?.spec_path ? ` · 定稿 ${row.spec_path}` : ' · 还没有定稿'}
            {row?.render_path ? ` · 渲染图 ${row.render_path}` : ''}
          </Typography.Text>
        </Space>
      </Card>

      <ChatPanel
        agentCode={WRITER}
        targetKind="character"
        targetRef={id}
        title={row ? `${row.name} 设定对焦` : undefined}
        onActiveChange={setConversationId}
        handoff={handoff}
      />

      <Row gutter={16}>
        <Col span={14}>
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            {row && (
              <SpecGateCard
                character={row}
                conversationId={conversationId}
                onHandoff={setHandoff}
              />
            )}
            {row && row.gate_spec_confirmed_at !== null && <RenderGateCard character={row} />}
          </Space>
        </Col>
        <Col span={10}>
          <EventTimeline events={events.data ?? []} loading={events.isLoading} />
        </Col>
      </Row>
    </Space>
  )
}

/**
 * 这个角色身上发生过的事。
 *
 * 新的在上：用户来看这里通常是想知道「刚才那次评审说了什么」。裁决全文很长，收起来放，展开
 * 才看——但一个字都不删。
 */
function EventTimeline({ events, loading }: { events: TaskEvent[]; loading: boolean }) {
  return (
    <Card size="small" title="这个角色发生过什么" loading={loading}>
      {events.length === 0 ? (
        <Empty image={null} description="评审与门禁的记录会留在这里。" />
      ) : (
        <div style={{ maxHeight: 420, overflowY: 'auto', paddingTop: 4 }}>
          <Timeline
            items={[...events].reverse().map((one) => ({
              key: one.seq,
              color: LEVEL_COLORS[one.level] ?? 'gray',
              children: (
                <Space direction="vertical" size={2} style={{ width: '100%' }}>
                  <Space size={6}>
                    <Typography.Text strong style={{ fontSize: 13 }}>
                      {one.event}
                    </Typography.Text>
                    <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                      {one.ts.replace('T', ' ').slice(0, 19)}
                    </Typography.Text>
                  </Space>
                  <Typography.Paragraph
                    style={{ fontSize: 12, marginBottom: 0 }}
                    ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}
                  >
                    <span style={{ whiteSpace: 'pre-wrap' }}>{one.message}</span>
                  </Typography.Paragraph>
                </Space>
              ),
            }))}
          />
        </div>
      )}
    </Card>
  )
}
