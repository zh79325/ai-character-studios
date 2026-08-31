/**
 * 项目首页 = 立项对焦页。
 *
 * 这一页只有一件事：跟设计师聊「这个项目要什么」。所以整页就是那个对话框，边上一条窄栏放待办，
 * 项目抬头也摆在那条窄栏里，其余入口都在顶栏菜单里，不在这儿再摆一遍。
 *
 * 「确认立项」是这一页唯一的收口动作：草稿落盘与定下名字与代号一并做完，不让用户先点一次沉淀。名字
 * 与代号不由用户凭空填，设计师聊出轮廓后会给几组建议（`[项目命名建议]`），用户点一条进表单
 * 或自己重写。
 *
 * 对焦会话由系统管（`managed`）：进页就接上这个项目还开着的那场，没有就开一场，开场先报一遍
 * 项目现状。
 */
import { CheckCircleOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Form, Input, Modal, Space, Tag, Typography } from 'antd'
import { useState } from 'react'

import { commitConversation, readConversation } from '@/api/conversations'
import { finalizeProject } from '@/api/projects'
import ChatPanel from '@/components/ChatPanel'
import ProjectFrame, { useCurrentProject } from '@/components/ProjectFrame'
import type { Draft, NamingOption, ProjectList } from '@/types/api'

export default function ProjectPage() {
  const current = useCurrentProject()
  const [conversation, setConversation] = useState<string | null>(null)
  const drafting = current.data?.stage === 'drafting'

  // 与 ChatPanel 共用一个 key，看的就是它已经拉回来的那份详情
  const detail = useQuery({
    queryKey: ['conversation', conversation],
    queryFn: () => readConversation(conversation!),
    enabled: conversation !== null && drafting,
  })

  return (
    <ProjectFrame header={false}>
      <ChatPanel
        agentCode="game_designer"
        targetKind="project"
        title="立项对焦"
        managed
        draftsAside
        commitInTodo={drafting}
        status={<ProjectStatus />}
        todo={
          drafting ? (
            <FinalizeTodo
              conversationId={conversation}
              drafts={detail.data?.drafts ?? []}
              naming={detail.data?.naming ?? []}
            />
          ) : null
        }
        onActiveChange={setConversation}
      />
    </ProjectFrame>
  )
}

/** 待办栏抬头：项目名、代号、阶段与目录，一眼能看完就不单占一张卡片。 */
function ProjectStatus() {
  const current = useCurrentProject()
  const project = current.data

  return (
    <Space direction="vertical" size={2} style={{ width: '100%' }}>
      <Space size={6} wrap>
        <Typography.Text strong>{project?.name ?? '…'}</Typography.Text>
        {project && <Tag>{project.code}</Tag>}
        {project?.stage === 'drafting' && <Tag color="processing">立项中</Tag>}
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
  )
}

/** 待办里的立项收口：选一组名字与代号，把草稿落盘、把目录骨架铺下。 */
function FinalizeTodo({
  conversationId,
  drafts,
  naming,
}: {
  conversationId: string | null
  drafts: Draft[]
  naming: NamingOption[]
}) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm<{ name: string; code: string }>()

  // 基线过期的那几份后端会拒收，跳开它们：立项不该被一份对不上基线的草稿卡住
  const landing = drafts.filter((one) => !one.stale).map((one) => one.id)

  const finalize = useMutation({
    mutationFn: async (values: { name: string; code: string }) => {
      // 先落盘再改名：代号一变项目就是另一个身份了，那时再去沉淀旧会话的草稿就对不上位置
      if (conversationId !== null && landing.length > 0) {
        await commitConversation(conversationId, landing)
      }
      return finalizeProject({ name: values.name.trim(), code: values.code.trim() })
    },
    onSuccess: (fresh: ProjectList) => {
      message.success(
        landing.length > 0
          ? `立项完成，${landing.length} 份草稿已落盘，目录骨架与 git 规则也铺好了`
          : '立项完成，目录骨架与 git 规则已经铺好',
      )
      queryClient.setQueryData(['projects'], fresh)
      // 代号变了等于换了个项目身份，缓存里跟项目有关的东西一律重取
      void queryClient.invalidateQueries()
      setOpen(false)
    },
    onError: (err: Error) => message.error(err.message),
  })

  return (
    <Space direction="vertical" size={6} style={{ width: '100%' }}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        聊定了就收口：定下名字与代号，草稿一并落盘，铺出素材目录。
      </Typography.Text>
      <Button block type="primary" icon={<CheckCircleOutlined />} onClick={() => setOpen(true)}>
        确认立项
        {naming.length > 0 && `（${naming.length} 组建议）`}
      </Button>
      <Modal
        open={open}
        title="确认立项"
        okText="确认立项"
        cancelText="再聊聊"
        confirmLoading={finalize.isPending}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {naming.length > 0 && (
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                设计师给的建议，点一条填进表单：
              </Typography.Text>
              {naming.map((one) => (
                <Typography.Link
                  key={`${one.name}/${one.code}`}
                  onClick={() => form.setFieldsValue({ name: one.name, code: one.code })}
                >
                  <Space size={6} wrap>
                    <Typography.Text strong>{one.name}</Typography.Text>
                    {one.code ? <Tag>{one.code}</Tag> : <Tag color="warning">代号待你填</Tag>}
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {one.reason}
                    </Typography.Text>
                  </Space>
                </Typography.Link>
              ))}
            </Space>
          )}
          <Form
            form={form}
            layout="vertical"
            requiredMark="optional"
            onFinish={(values) => finalize.mutate(values)}
          >
            <Form.Item
              name="name"
              label="项目名"
              rules={[{ required: true, message: '得给个名字' }]}
            >
              <Input placeholder="赤瞳系列" />
            </Form.Item>
            <Form.Item
              name="code"
              label="项目代号"
              extra="会进日志、提示词与外部接口参数，所以只收小写英文、数字、- 和 _"
              style={{ marginBottom: 0 }}
              rules={[
                { required: true, message: '得给个代号' },
                { pattern: /^[a-z0-9][a-z0-9_-]*$/, message: '只能用小写英文、数字、- 和 _' },
              ]}
            >
              <Input placeholder="chitong" />
            </Form.Item>
          </Form>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            提交后把待确认的草稿写进项目目录，再铺素材目录与 `.gitignore`、`.gitattributes`
            （图片与模型走 LFS）。
          </Typography.Text>
        </Space>
      </Modal>
    </Space>
  )
}
