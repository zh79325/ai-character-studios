/**
 * 项目首页 = 立项对焦页。
 *
 * 打开项目先落在这里，因为「这个项目要什么」是所有后续工作的前提。立项中就在这儿把它聊出来
 * 并收口；已立项也还在这儿聊——项目要求本来就会随着做下去而改，改完照样是这场对话的产物。
 *
 * 名字与代号不由用户凭空填：设计师聊出轮廓后会给几组建议（`[项目命名建议]`），用户点一条
 * 进表单或自己重写，再点「完成立项」，后端这时才铺目录骨架与 git 规则。
 *
 * 对焦会话由系统管（`managed`）：进页就接上这个项目还开着的那场，没有就开一场，开场先报一遍
 * 项目现状。用户在这页要做的只有一件事：说自己想要什么。
 */
import { CheckCircleOutlined, RightOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Col, Empty, Form, Input, Row, Space, Tag, Typography } from 'antd'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { readConversation } from '@/api/conversations'
import { finalizeProject } from '@/api/projects'
import ChatPanel from '@/components/ChatPanel'
import ProjectFrame, { useCurrentProject } from '@/components/ProjectFrame'
import { DESIGN_ENTRIES, designPath } from '@/lib/design'
import type { NamingOption, ProjectList } from '@/types/api'

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
    <ProjectFrame>
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {drafting ? <FinalizePanel naming={detail.data?.naming ?? []} /> : <NextSteps />}
        <ChatPanel
          agentCode="game_designer"
          targetKind="project"
          title="立项对焦"
          managed
          onActiveChange={setConversation}
        />
      </Space>
    </ProjectFrame>
  )
}

/** 立项收口面板：选一组名字与代号，落下目录骨架。 */
function FinalizePanel({ naming }: { naming: NamingOption[] }) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [form] = Form.useForm<{ name: string; code: string }>()

  const finalize = useMutation({
    mutationFn: (values: { name: string; code: string }) =>
      finalizeProject({ name: values.name.trim(), code: values.code.trim() }),
    onSuccess: (fresh: ProjectList) => {
      message.success('立项完成，目录骨架与 git 规则已经铺好')
      queryClient.setQueryData(['projects'], fresh)
      // 代号变了等于换了个项目身份，缓存里跟项目有关的东西一律重取
      void queryClient.invalidateQueries()
    },
    onError: (err: Error) => message.error(err.message),
  })

  return (
    <Card size="small" title="立项收口">
      <Row gutter={16}>
        <Col span={14}>
          {naming.length === 0 ? (
            <Empty
              image={null}
              description="先跟设计师聊题材与美术方向，聊出轮廓后他会给几组名字与代号建议；你也可以直接自己填。"
            />
          ) : (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                设计师给的建议，点一条填进右边表单：
              </Typography.Text>
              {naming.map((one) => (
                <Card
                  key={`${one.name}/${one.code}`}
                  size="small"
                  hoverable
                  onClick={() => form.setFieldsValue({ name: one.name, code: one.code })}
                >
                  <Space size={8} wrap>
                    <Typography.Text strong>{one.name}</Typography.Text>
                    {one.code ? <Tag>{one.code}</Tag> : <Tag color="warning">代号待你填</Tag>}
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {one.reason}
                    </Typography.Text>
                  </Space>
                </Card>
              ))}
            </Space>
          )}
        </Col>
        <Col span={10}>
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
              rules={[
                { required: true, message: '得给个代号' },
                { pattern: /^[a-z0-9][a-z0-9_-]*$/, message: '只能用小写英文、数字、- 和 _' },
              ]}
            >
              <Input placeholder="chitong" />
            </Form.Item>
            <Button
              type="primary"
              block
              icon={<CheckCircleOutlined />}
              htmlType="submit"
              loading={finalize.isPending}
            >
              完成立项
            </Button>
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8 }}>
              点下去才铺素材目录、`.gitignore` 与 `.gitattributes`（图片与模型走 LFS）；
              聊出来的视觉规范不会被覆盖。
            </Typography.Paragraph>
          </Form>
        </Col>
      </Row>
    </Card>
  )
}

/** 已立项后的推荐操作：各类素材设计的入口。 */
function NextSteps() {
  const navigate = useNavigate()

  return (
    <Card size="small" title="接下来做什么">
      <Row gutter={[12, 12]}>
        {DESIGN_ENTRIES.map((entry) => (
          <Col key={entry.slug} span={6}>
            <Card
              size="small"
              hoverable
              onClick={() => navigate(designPath(entry.slug))}
              style={{ height: '100%' }}
            >
              <Space direction="vertical" size={2}>
                <Space size={6}>
                  <Typography.Text strong>{entry.label}</Typography.Text>
                  {entry.ready ? <RightOutlined /> : <Tag>即将开放</Tag>}
                </Space>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {entry.hint}
                </Typography.Text>
              </Space>
            </Card>
          </Col>
        ))}
      </Row>
      <Alert
        type="info"
        showIcon
        style={{ marginTop: 12 }}
        message="想调整项目要求就接着在下面聊，聊出来的规范确认后照样会沉淀进视觉规范与项目配置。"
      />
    </Card>
  )
}
