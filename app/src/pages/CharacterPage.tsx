/**
 * 一个角色的工作台。
 *
 * 页面只留两块：左边是角色设计会话，右边是已经明确的角色信息。需要用户拍板的分歧由会话里的
 * 待选项抽屉承接；右栏只展示角色状态、已经沉淀的角色记忆和定稿图片，不再重复摆评审、门禁与
 * 事件时间线。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Card, Collapse, Empty, Image, Space, Tag, Typography } from 'antd'
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { listRenders, listViews, readCharacter, renderImageUrl } from '@/api/characters'
import { commitConversation, listMemories, readConversation } from '@/api/conversations'
import { ApiError } from '@/api/client'
import ChatPanel from '@/components/chat'
import MarkdownText from '@/components/MarkdownText'
import ProjectFrame from '@/components/ProjectFrame'
import type { Character, Draft, Generation, ProjectMemoryItem } from '@/types/api'

const WRITER = 'spec_writer'

const MEMORY_LABELS: Record<string, string> = {
  preference: '偏好',
  taboo: '禁忌',
  fact: '事实',
}

const VIEW_LABELS: Record<string, string> = {
  front: '正面',
  right: '右侧 30°',
  back: '背面',
  left: '左侧 30°',
}

export default function CharacterPage() {
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [conversationId, setConversationId] = useState<string | null>(null)

  const character = useQuery({
    queryKey: ['character', id],
    queryFn: () => readCharacter(id),
    enabled: id !== '',
  })
  const detail = useQuery({
    queryKey: ['conversation', conversationId],
    queryFn: () => readConversation(conversationId!),
    enabled: conversationId !== null,
  })

  if (character.error instanceof ApiError && character.error.status === 404) {
    return (
      <ProjectFrame breadcrumb={[{ label: '角色设计', path: '/design/characters' }]}>
        <Card>
          <Empty description="这个角色不在当前项目里。可能是切过项目，或者它已经被移出。">
            <Typography.Link onClick={() => navigate('/project')}>回到当前项目</Typography.Link>
          </Empty>
        </Card>
      </ProjectFrame>
    )
  }

  const row = character.data
  const drafts = (detail.data?.drafts ?? []).filter((one) => !one.stale)
  const finaleKey = drafts.length > 0 ? `spec:${drafts.map((one) => one.id).join(',')}` : ''

  return (
    <ProjectFrame
      requireReady
      breadcrumb={[
        { label: '角色设计', path: '/design/characters' },
        { label: row?.name ?? '角色' },
      ]}
    >
      <ChatPanel
        agentCode={WRITER}
        targetKind="character"
        targetRef={id}
        title={row ? `${row.name} 设定对焦` : undefined}
        onActiveChange={setConversationId}
        heading="设定对焦"
        who="角色设计师"
        draftsAside
        sidebar={
          row ? (
            <CharacterSidebar character={row} decisions={detail.data?.memory.decisions ?? []} />
          ) : null
        }
        finaleTitle="确认角色设定"
        finaleKey={finaleKey}
        finale={
          drafts.length === 0
            ? null
            : () => <CharacterDraftGate conversationId={conversationId} drafts={drafts} />
        }
        starters={row ? [`帮我设计一个符合当前项目要求的角色，名字叫${row.name}`] : []}
      />
    </ProjectFrame>
  )
}

function CharacterDraftGate({
  conversationId,
  drafts,
}: {
  conversationId: string | null
  drafts: Draft[]
}) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const commit = useMutation({
    mutationFn: () =>
      commitConversation(
        conversationId!,
        drafts.map((one) => one.id),
      ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['conversation', conversationId] }),
        queryClient.invalidateQueries({ queryKey: ['character'] }),
        queryClient.invalidateQueries({ queryKey: ['memories'] }),
      ])
      message.success('角色设定已写入项目目录')
    },
    onError: (error: Error) => message.error(error.message),
  })

  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Typography.Text strong style={{ fontSize: 13 }}>
        确认角色设定
      </Typography.Text>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        确认后写入角色目录；需要修改就关掉抽屉，继续在左侧对话。
      </Typography.Text>
      <Collapse
        size="small"
        defaultActiveKey={drafts[0]?.id}
        items={drafts.map((one) => ({
          key: one.id,
          label: <Typography.Text style={{ fontSize: 12 }}>{one.target_path}</Typography.Text>,
          children: <MarkdownText text={one.content} />,
        }))}
      />
      <Button
        type="primary"
        block
        disabled={conversationId === null}
        loading={commit.isPending}
        onClick={() => commit.mutate()}
      >
        确认角色设定
      </Button>
    </Space>
  )
}

function CharacterSidebar({ character, decisions }: { character: Character; decisions: string[] }) {
  const memories = useQuery({
    queryKey: ['memories', character.id],
    queryFn: () => listMemories(character.id),
  })
  const renders = useQuery({
    queryKey: ['character-renders', character.id],
    queryFn: () => listRenders(character.id),
  })
  const views = useQuery({
    queryKey: ['character-views', character.id],
    queryFn: () => listViews(character.id),
  })

  const ownMemories = (memories.data ?? []).filter(
    (one) => one.character_ref === character.id && one.enabled,
  )
  const finalRenders = (renders.data ?? []).filter((one) => one.is_final)
  const finalViews = (views.data ?? []).filter((one) => one.is_final)

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Card size="small" title="角色信息">
        <Space direction="vertical" size={5} style={{ width: '100%' }}>
          <Space size={6} wrap>
            <Typography.Text strong>{character.name}</Typography.Text>
            <Tag color="processing">{character.state_label}</Tag>
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12, wordBreak: 'break-all' }}>
            {character.dir_name}
          </Typography.Text>
        </Space>
      </Card>
      <MemoryCard decisions={decisions} memories={ownMemories} loading={memories.isLoading} />
      <PreviewCard
        characterId={character.id}
        renders={finalRenders}
        views={finalViews}
        loading={renders.isLoading || views.isLoading}
      />
    </Space>
  )
}

function MemoryCard({
  decisions,
  memories,
  loading,
}: {
  decisions: string[]
  memories: ProjectMemoryItem[]
  loading: boolean
}) {
  const remembered = new Set(memories.map((one) => one.content))
  const settled = decisions.filter((one) => !remembered.has(one))
  const empty = settled.length === 0 && memories.length === 0

  return (
    <Card size="small" title="角色记忆" loading={loading}>
      {empty ? (
        <Empty image={null} description="聊定并沉淀的角色信息会出现在这里。" />
      ) : (
        <Space direction="vertical" size={7} style={{ width: '100%' }}>
          {settled.map((content) => (
            <MemoryRow key={`settled:${content}`} label="已定" content={content} />
          ))}
          {memories.map((one) => (
            <MemoryRow
              key={one.id}
              label={MEMORY_LABELS[one.kind] ?? one.kind}
              content={one.content}
            />
          ))}
        </Space>
      )}
    </Card>
  )
}

function MemoryRow({ label, content }: { label: string; content: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-start', gap: 6 }}>
      <Tag style={{ margin: 0, flex: '0 0 auto' }}>{label}</Tag>
      <Typography.Text style={{ fontSize: 12 }}>{content}</Typography.Text>
    </div>
  )
}

function PreviewCard({
  characterId,
  renders,
  views,
  loading,
}: {
  characterId: string
  renders: Generation[]
  views: Generation[]
  loading: boolean
}) {
  const images = [
    ...renders.map((row) => ({ row, label: '角色渲染图' })),
    ...views.map((row) => ({
      row,
      label: VIEW_LABELS[row.variant ?? ''] ?? row.variant ?? '视图',
    })),
  ]

  return (
    <Card size="small" title="已定稿图片" loading={loading}>
      {images.length === 0 ? (
        <Empty image={null} description="定稿后的渲染图与四视图会出现在这里。" />
      ) : (
        <Image.PreviewGroup>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {images.map(({ row, label }) => (
              <PreviewImage key={row.id} characterId={characterId} row={row} label={label} />
            ))}
          </div>
        </Image.PreviewGroup>
      )}
    </Card>
  )
}

function PreviewImage({
  characterId,
  row,
  label,
}: {
  characterId: string
  row: Generation
  label: string
}) {
  const url = useQuery({
    queryKey: ['character-image-url', characterId, row.id],
    queryFn: () => renderImageUrl(characterId, row.id),
  })

  return (
    <Space direction="vertical" size={2} align="center">
      {url.data ? (
        <Image src={url.data} width={116} height={116} style={{ objectFit: 'cover' }} />
      ) : (
        <div style={{ width: 116, height: 116, borderRadius: 4, background: '#f5f5f5' }} />
      )}
      <Typography.Text type="secondary" style={{ fontSize: 11 }}>
        {label}
      </Typography.Text>
    </Space>
  )
}
