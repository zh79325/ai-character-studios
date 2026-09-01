/**
 * 一个角色的工作台。
 *
 * 页面只留两块：左边是角色设计会话，右边是已经明确的角色信息。需要用户拍板的分歧由会话里的
 * 待选项抽屉承接；右栏只展示角色状态、已经沉淀的角色记忆和定稿图片，不再重复摆评审、门禁与
 * 事件时间线。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Card, Collapse, Empty, Image, Space, Tag, Typography } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import {
  listRenders,
  listViews,
  readCharacter,
  renderCharacter,
  renderImageUrl,
} from '@/api/characters'
import { commitConversation, listMemories, readConversation } from '@/api/conversations'
import { ApiError } from '@/api/client'
import ChatPanel from '@/components/chat'
import MarkdownText from '@/components/MarkdownText'
import ProjectFrame from '@/components/ProjectFrame'
import RenderDecisionGate from '@/components/RenderDecisionGate'
import { designPath } from '@/lib/design'
import { projectPath, useProjectCode } from '@/lib/projectRoute'
import type { Attachment, Character, Draft, Generation, ProjectMemoryItem } from '@/types/api'

const WRITER = 'spec_writer'

/** 「效果图已生成」这一档：设定落盘后画师自动出图会把状态推到这里。 */
const RENDER_GENERATED = 'S2_render_generated'

const MEMORY_LABELS: Record<string, string> = {
  preference: '偏好',
  taboo: '禁忌',
  fact: '事实',
}

const VIEW_LABELS: Record<string, string> = {
  sheet: '四视图',
  front: '正面',
  right: '右侧 30°',
  back: '背面',
  left: '左侧 30°',
}

export default function CharacterPage() {
  const projectCode = useProjectCode()
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [conversationId, setConversationId] = useState<string | null>(null)

  const character = useQuery({
    queryKey: ['project', projectCode, 'character', id],
    queryFn: () => readCharacter(projectCode, id),
    enabled: id !== '',
  })
  const detail = useQuery({
    queryKey: ['project', projectCode, 'conversation', conversationId],
    queryFn: () => readConversation(projectCode, conversationId!),
    enabled: conversationId !== null,
  })

  const queryClient = useQueryClient()
  const { message } = App.useApp()
  const renders = useQuery({
    queryKey: ['project', projectCode, 'character-renders', id],
    queryFn: () => listRenders(projectCode, id),
    enabled: id !== '',
  })

  // 设定一落盘，画师就自动出第一版效果图，用户不用再手点「生成」。后端同步落一条
  // thinking 画师消息后才回，所以触发后立刻刷一次会话把它取出来，进度交给 useConversation 订流。
  const fireRender = useMutation({
    mutationFn: () => renderCharacter(projectCode, id, '', ''),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['project', projectCode, 'conversation'] }),
        queryClient.invalidateQueries({
          queryKey: ['project', projectCode, 'character-renders', id],
        }),
        queryClient.invalidateQueries({ queryKey: ['project', projectCode, 'character', id] }),
      ])
    },
    onError: (error: Error) => message.error(error.message),
  })

  const busy = (detail.data?.messages ?? []).some((one) => one.status === 'thinking')
  const hasRenderCandidate = (renders.data?.length ?? 0) > 0
  // 一个角色只自动首生一次：重画由用户在收口抽屉点，别让 effect 的依赖抖动又开一张
  const firedFor = useRef<string | null>(null)
  useEffect(() => {
    const row = character.data
    if (!row || row.spec_path === null) return
    if (row.state >= RENDER_GENERATED || hasRenderCandidate || busy) return
    if (!renders.isSuccess || firedFor.current === row.id) return
    firedFor.current = row.id
    fireRender.mutate()
    // 后端已同步落一条 thinking 画师消息：立刻刷会话把它取出来，useConversation 会接手订流
    void queryClient.invalidateQueries({ queryKey: ['project', projectCode, 'conversation'] })
  }, [
    character.data,
    renders.isSuccess,
    hasRenderCandidate,
    busy,
    projectCode,
    queryClient,
    fireRender,
  ])

  // 画师往会话里塞的效果图存的是相对 API 路径，渲染进程读不到磁盘，得换成带 baseUrl 的绝对地址
  const resolveImageUrl = useCallback(
    (att: Attachment) =>
      att.generation_id !== undefined
        ? renderImageUrl(projectCode, id, att.generation_id)
        : Promise.resolve(att.url ?? ''),
    [projectCode, id],
  )

  if (character.error instanceof ApiError && character.error.status === 404) {
    return (
      <ProjectFrame
        breadcrumb={[{ label: '角色设计', path: designPath(projectCode, 'characters') }]}
      >
        <Card>
          <Empty description="这个角色不在该项目里，可能已被移出。">
            <Typography.Link onClick={() => navigate(projectPath(projectCode))}>
              回到项目首页
            </Typography.Link>
          </Empty>
        </Card>
      </ProjectFrame>
    )
  }

  const row = character.data
  const drafts = (detail.data?.drafts ?? []).filter((one) => !one.stale)
  // 同一时刻只一个收口：有待确认 spec 草稿优先走确认设定，否则最新那张效果图没定稿就收成「采用/再画」
  const latestRender = renders.data?.[0]
  const inRenderReview =
    drafts.length === 0 &&
    row?.state === RENDER_GENERATED &&
    latestRender !== undefined &&
    !latestRender.is_final
  const finaleTitle = drafts.length > 0 ? '确认角色设定' : '这张效果图'
  const finaleKey =
    drafts.length > 0
      ? `spec:${drafts.map((one) => one.id).join(',')}`
      : inRenderReview
        ? `render:${latestRender!.id}`
        : ''

  return (
    <ProjectFrame
      requireReady
      breadcrumb={[
        { label: '角色设计', path: designPath(projectCode, 'characters') },
        { label: row?.name ?? '角色' },
      ]}
    >
      <ChatPanel
        projectCode={projectCode}
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
            <CharacterSidebar
              projectCode={projectCode}
              character={row}
              decisions={detail.data?.memory.decisions ?? []}
            />
          ) : null
        }
        finaleTitle={finaleTitle}
        finaleKey={finaleKey}
        resolveImageUrl={resolveImageUrl}
        finale={
          drafts.length > 0
            ? () => (
                <CharacterDraftGate
                  projectCode={projectCode}
                  conversationId={conversationId}
                  drafts={drafts}
                />
              )
            : inRenderReview
              ? () => (
                  <RenderDecisionGate
                    projectCode={projectCode}
                    characterId={id}
                    generationId={latestRender!.id}
                  />
                )
              : null
        }
        starters={row ? [`帮我设计一个符合当前项目要求的角色，名字叫${row.name}`] : []}
      />
    </ProjectFrame>
  )
}

function CharacterDraftGate({
  projectCode,
  conversationId,
  drafts,
}: {
  projectCode: string
  conversationId: string | null
  drafts: Draft[]
}) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const commit = useMutation({
    mutationFn: () =>
      commitConversation(
        projectCode,
        conversationId!,
        drafts.map((one) => one.id),
      ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ['project', projectCode, 'conversation', conversationId],
        }),
        queryClient.invalidateQueries({ queryKey: ['project', projectCode, 'character'] }),
        queryClient.invalidateQueries({ queryKey: ['project', projectCode, 'memories'] }),
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

function CharacterSidebar({
  projectCode,
  character,
  decisions,
}: {
  projectCode: string
  character: Character
  decisions: string[]
}) {
  const memories = useQuery({
    queryKey: ['project', projectCode, 'memories', character.id],
    queryFn: () => listMemories(projectCode, character.id),
  })
  const renders = useQuery({
    queryKey: ['project', projectCode, 'character-renders', character.id],
    queryFn: () => listRenders(projectCode, character.id),
  })
  const views = useQuery({
    queryKey: ['project', projectCode, 'character-views', character.id],
    queryFn: () => listViews(projectCode, character.id),
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
        projectCode={projectCode}
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
  projectCode,
  characterId,
  renders,
  views,
  loading,
}: {
  projectCode: string
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
              <PreviewImage
                key={row.id}
                projectCode={projectCode}
                characterId={characterId}
                row={row}
                label={label}
              />
            ))}
          </div>
        </Image.PreviewGroup>
      )}
    </Card>
  )
}

function PreviewImage({
  projectCode,
  characterId,
  row,
  label,
}: {
  projectCode: string
  characterId: string
  row: Generation
  label: string
}) {
  const url = useQuery({
    queryKey: ['project', projectCode, 'character-image-url', characterId, row.id],
    queryFn: () => renderImageUrl(projectCode, characterId, row.id),
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
