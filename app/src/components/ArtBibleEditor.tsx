/**
 * 视觉规范（art bible）编辑器。
 *
 * 它是一份 markdown，整篇覆盖保存——不做行级合并，因为它是视觉真相，半份合并出来的规范
 * 比冲突更危险。
 *
 * 右边那列「风格禁止项」不是装饰：后端从「## 6 风格禁止项」一节抽条目，生图时拼进
 * negative prompt。所以这里显示的就是真正会生效的那份清单，写完立刻能看出有没有被抽到。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Col, Input, Row, Space, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'

import { readArtBible, writeArtBible } from '@/api/projects'

export default function ArtBibleEditor() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const bible = useQuery({ queryKey: ['art-bible'], queryFn: () => readArtBible() })
  const [draft, setDraft] = useState<string | null>(null)

  useEffect(() => {
    // 读到新内容就丢掉草稿：这条路径只有首次加载和用户主动「放弃修改」会走
    setDraft(null)
  }, [bible.data])

  const content = draft ?? bible.data?.content ?? ''
  const dirty = draft !== null && draft !== bible.data?.content

  const save = useMutation({
    mutationFn: () => writeArtBible(content),
    onSuccess: (fresh) => {
      message.success('视觉规范已保存')
      queryClient.setQueryData(['art-bible'], fresh)
      setDraft(null)
    },
    onError: (err: Error) => message.error(err.message),
  })

  return (
    <Row gutter={16}>
      <Col span={16}>
        <Card
          size="small"
          title="art-bible.md"
          loading={bible.isLoading}
          extra={
            <Space>
              <Button disabled={!dirty} onClick={() => setDraft(null)}>
                放弃修改
              </Button>
              <Button
                type="primary"
                loading={save.isPending}
                disabled={!dirty}
                onClick={() => save.mutate()}
              >
                保存
              </Button>
            </Space>
          }
        >
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Input.TextArea
              value={content}
              rows={26}
              spellCheck={false}
              style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 13 }}
              onChange={(event) => setDraft(event.target.value)}
            />
            <Typography.Text type="secondary" style={{ fontSize: 12 }} copyable={!!bible.data}>
              {bible.data?.path ?? ''}
            </Typography.Text>
          </Space>
        </Card>
      </Col>
      <Col span={8}>
        <Card size="small" title="会进 negative prompt 的禁止项">
          {bible.data?.forbidden.length ? (
            <Space size={[6, 6]} wrap>
              {bible.data.forbidden.map((term) => (
                <Tag key={term} color="volcano">
                  {term}
                </Tag>
              ))}
            </Space>
          ) : (
            <Alert
              type="info"
              showIcon
              message="还没有生效的禁止项"
              description="在「## 6 风格禁止项」一节下面按列表写，一行一条；模板里的「待填」不算。"
            />
          )}
          {dirty && (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 12 }}
              message="这份清单是上次保存的内容"
              description="保存之后才会重新抽取。"
            />
          )}
        </Card>
      </Col>
    </Row>
  )
}
