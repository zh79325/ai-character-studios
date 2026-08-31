/**
 * 项目首页 = 立项对焦页。
 *
 * 这一页只有一件事：跟设计师聊「这个项目要什么」。所以整页就是那个对话框，边上那条窄栏只放项目
 * 抬头与后续动作的入口，其余入口都在顶栏菜单里，不在这儿再摆一遍。
 *
 * 收口的两步都在待选项抽屉里，跟这一轮的题排在一起：
 *
 * 1. **确认游戏风格**：抽屉里摆设计师写的那份 art-bible 全文，看完再拍——以前这一步叫「确认沉淀」，
 *    用户既看不出沉淀的是什么，也不知道点完会发生什么。
 * 2. **确认立项**：定下项目名与代号。落盘之后自动向设计师要几组命名建议（`[项目命名建议]`），
 *    用户点一组或自己写，再按确认立项。名字与代号不该让用户凭空填，聊过一路的是设计师。
 *
 * 两步的交互跟拍待选项一模一样：新东西到了抽屉自己弹出来，拍一下就过，不想现在拍就关掉接着聊，
 * 聊完再从那个按钮抽回来。
 *
 * 对焦会话由系统管（`managed`）：进页就接上这个项目还开着的那场，没有就开一场，开场先报一遍
 * 项目现状。
 */
import { RightOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Card, Collapse, Input, Radio, Space, Tag, Typography } from 'antd'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { commitConversation, readConversation } from '@/api/conversations'
import { finalizeProject } from '@/api/projects'
import ChatPanel from '@/components/ChatPanel'
import { Row, ROW_LABEL } from '@/components/ChoicePicker'
import MarkdownText from '@/components/MarkdownText'
import ProjectFrame, { useCurrentProject } from '@/components/ProjectFrame'
import { DESIGN_ENTRIES, designPath } from '@/lib/design'
import type { Draft, NamingOption, ProjectList } from '@/types/api'

/** 落盘完向设计师要命名建议的那句话：由界面替用户说，因为下一步的表单等着它的回话。 */
const ASK_NAMING = '风格就按这版定了，已经落盘。给我几组项目名与代号，我选一组确认立项。'

const CODE_RULE = /^[a-z0-9][a-z0-9_-]*$/

/** 开场那句话的示例：新项目进来还没开口时摆在输入框上面，点一下填进去。 */
const STARTER =
  '我要开发一款类似我的世界地下城的刷怪RPG，玩家扮演的角色是西游记中的人物例如孙悟空，' +
  '猪八戒，二郎神等，怪物是类似奥特曼电视剧中的怪兽，场景是在现代各个城市的地标建筑附近。'

export default function ProjectPage() {
  const current = useCurrentProject()
  const [conversation, setConversation] = useState<string | null>(null)
  // 落盘完那一瞬草稿就没了，而设计师的命名建议还在路上：不记一笔这中间一段就既无风格也无立项
  const [settled, setSettled] = useState(false)
  const drafting = current.data?.stage === 'drafting'

  // 与 ChatPanel 共用一个 key，看的就是它已经拉回来的那份详情
  const detail = useQuery({
    queryKey: ['conversation', conversation],
    queryFn: () => readConversation(conversation!),
    enabled: conversation !== null && drafting,
  })

  // 基线过期的那几份后端会拒收，跳开它们：立项不该被一份对不上基线的草稿卡住
  const landing = (detail.data?.drafts ?? []).filter((one) => !one.stale)
  const naming = detail.data?.naming ?? []

  // 聊到有东西可拍才摆收口：白纸一张就摆一个「确认立项」，等于请用户跳过聊风格这一步
  const gate = landing.length > 0 ? 'style' : naming.length > 0 || settled ? 'launch' : null
  // 换了一份草稿、换了一批建议就重新弹出来；同一份反复刷详情不打扰已经关掉它的人
  const finaleKey =
    gate === 'style'
      ? `style:${landing.map((one) => one.id).join(',')}`
      : gate === 'launch'
        ? `launch:${naming.map((one) => `${one.name}/${one.code}`).join(',')}`
        : ''

  return (
    <ProjectFrame header={false}>
      <ChatPanel
        agentCode="game_designer"
        targetKind="project"
        title="立项对焦"
        managed
        draftsAside
        sidebar={<Sidebar />}
        starter={STARTER}
        finaleTitle={gate === 'style' ? '确认游戏风格' : '确认立项'}
        finaleKey={!drafting ? '' : finaleKey}
        finale={
          !drafting || gate === null
            ? null
            : (say) =>
                gate === 'style' ? (
                  <StyleGate
                    conversationId={conversation}
                    drafts={landing}
                    say={say}
                    onDone={() => setSettled(true)}
                  />
                ) : (
                  <LaunchGate naming={naming} say={say} />
                )
        }
        onActiveChange={setConversation}
      />
    </ProjectFrame>
  )
}

/** 边上那条窄栏：项目抬头 + 后续动作的入口。草稿的去处已经在抽屉里，这里不再摆一遍。 */
function Sidebar() {
  const current = useCurrentProject()
  const project = current.data
  const navigate = useNavigate()
  const locked = project?.stage === 'drafting'

  return (
    <Card size="small" title="快捷导航">
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        <Space direction="vertical" size={2} style={{ width: '100%' }}>
          <Space size={6} wrap>
            <Typography.Text strong>{project?.name ?? '…'}</Typography.Text>
            {project && <Tag>{project.code}</Tag>}
            {locked && <Tag color="processing">立项中</Tag>}
            {project?.missing && <Tag color="error">目录不在</Tag>}
          </Space>
          <Typography.Text
            type="secondary"
            style={{ fontSize: 12, wordBreak: 'break-all' }}
            copyable={{ text: project?.dir_path ?? '' }}
          >
            {project?.dir_path ?? ''}
          </Typography.Text>
        </Space>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {locked ? '立项收口后开工：' : '接下去干什么：'}
        </Typography.Text>
        {DESIGN_ENTRIES.map((entry) => (
          <Button
            key={entry.slug}
            block
            size="small"
            disabled={locked || !entry.ready}
            title={entry.hint}
            style={{ textAlign: 'left' }}
            onClick={() => navigate(designPath(entry.slug))}
          >
            <Space size={4}>
              <RightOutlined style={{ fontSize: 10 }} />
              {entry.ready ? entry.label : `${entry.label}（即将开放）`}
            </Space>
          </Button>
        ))}
      </Space>
    </Card>
  )
}

/** 第一步：把设计师写的风格落盘。落完顺手替用户去要命名建议，不让他自己想起来该问。 */
function StyleGate({
  conversationId,
  drafts,
  say,
  onDone,
}: {
  conversationId: string | null
  drafts: Draft[]
  say: (text: string) => void
  onDone: () => void
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
      message.success(`风格已落盘（${drafts.length} 份），接着定项目名与代号`)
      onDone()
      await queryClient.invalidateQueries({ queryKey: ['conversation', conversationId] })
      say(ASK_NAMING)
    },
    onError: (err: Error) => message.error(err.message),
  })

  return (
    <Space direction="vertical" size={6} style={{ width: '100%' }}>
      <Typography.Text strong style={{ fontSize: 13 }}>
        确认游戏风格
      </Typography.Text>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        看一眼这几份，确认了就写进项目目录，之后设计师给项目名与代号；要改就关掉这层说改哪里。
      </Typography.Text>
      <Collapse
        size="small"
        defaultActiveKey={drafts[0]?.id}
        items={drafts.map((one) => ({
          key: one.id,
          label: <Typography.Text style={{ fontSize: 12 }}>{one.target_path}</Typography.Text>,
          children: <Preview draft={one} />,
        }))}
      />
      <Button
        type="primary"
        block
        disabled={conversationId === null}
        loading={commit.isPending}
        onClick={() => commit.mutate()}
      >
        确认游戏风格
      </Button>
    </Space>
  )
}

/** 「其他」那一档：选项文字里不可能出现控制字符，拿它当哨兵不会跟真的建议撞上。 */
const CUSTOM = '\u0000custom'

const MONO = 'ui-monospace, SFMono-Regular, Menlo, monospace'

/** 草稿全文。md 按 markdown 渲染；`project.json` 这种照原样摆，拿 markdown 读会把缩进与引号吃掉。 */
function Preview({ draft }: { draft: Draft }) {
  if (draft.target_path.endsWith('.md')) return <MarkdownText text={draft.content} />

  return (
    <pre
      style={{
        margin: 0,
        fontFamily: MONO,
        fontSize: 12,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-all',
      }}
    >
      {draft.content}
    </pre>
  )
}

/** 第二步：定名字与代号。一组建议一行，跟上面的题一个长相。 */
function LaunchGate({ naming, say }: { naming: NamingOption[]; say: (text: string) => void }) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [pick, setPick] = useState<string | null>(null)
  const [ownName, setOwnName] = useState('')
  const [ownCode, setOwnCode] = useState('')

  // 建议是后一轮才回来的，先不锁死选中项：默认第一组，用户点过就听他的
  const active = pick ?? (naming.length > 0 ? '0' : CUSTOM)
  const chosen = active === CUSTOM ? null : (naming[Number(active)] ?? null)
  const name = (chosen?.name ?? ownName).trim()
  // 建议里代号可以缺（设计师拿不准英文写法），缺的那组照样能选，代号自己补一个
  const code = (chosen === null || chosen.code === '' ? ownCode : chosen.code).trim()
  const ok = name !== '' && CODE_RULE.test(code)

  const finalize = useMutation({
    mutationFn: () => finalizeProject({ name, code }),
    onSuccess: (fresh: ProjectList) => {
      message.success('立项完成，目录骨架与 git 规则已经铺好')
      queryClient.setQueryData(['projects'], fresh)
      // 代号变了等于换了个项目身份，缓存里跟项目有关的东西一律重取
      void queryClient.invalidateQueries()
    },
    onError: (err: Error) => message.error(err.message),
  })

  return (
    <Space direction="vertical" size={6} style={{ width: '100%' }}>
      <Typography.Text strong style={{ fontSize: 13 }}>
        项目名与代号
      </Typography.Text>
      {naming.length === 0 && (
        <Space size={6} wrap>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            设计师还没给建议。
          </Typography.Text>
          <Typography.Link style={{ fontSize: 12 }} onClick={() => say('给我几组项目名与代号。')}>
            让它给几组
          </Typography.Link>
        </Space>
      )}
      <Radio.Group
        value={active}
        style={{ width: '100%' }}
        onChange={(event) => setPick(event.target.value as string)}
      >
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          {naming.map((one, index) => (
            <Row key={`${one.name}/${one.code}`} active={active === String(index)}>
              <Radio value={String(index)} style={ROW_LABEL}>
                <Space size={6} wrap>
                  <Typography.Text strong>{one.name}</Typography.Text>
                  {one.code ? <Tag>{one.code}</Tag> : <Tag color="warning">代号待你填</Tag>}
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {one.reason}
                  </Typography.Text>
                </Space>
              </Radio>
            </Row>
          ))}
          <Row active={active === CUSTOM}>
            <Radio value={CUSTOM} style={ROW_LABEL}>
              自己写
            </Radio>
          </Row>
        </Space>
      </Radio.Group>
      {chosen === null && (
        <Input
          size="small"
          value={ownName}
          placeholder="项目名，例如 赤瞳系列"
          onChange={(event) => setOwnName(event.target.value)}
        />
      )}
      {(chosen === null || chosen.code === '') && (
        <Input
          size="small"
          value={ownCode}
          placeholder="项目代号，只收小写英文、数字、- 和 _"
          onChange={(event) => setOwnCode(event.target.value)}
        />
      )}
      <Button
        type="primary"
        block
        disabled={!ok}
        loading={finalize.isPending}
        onClick={() => finalize.mutate()}
      >
        确认立项
      </Button>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {ok
          ? '按下去铺素材目录与 `.gitignore`、`.gitattributes`（图片与模型走 LFS）'
          : '代号会进日志、提示词与外部接口参数，所以只收小写英文、数字、- 和 _'}
      </Typography.Text>
    </Space>
  )
}
