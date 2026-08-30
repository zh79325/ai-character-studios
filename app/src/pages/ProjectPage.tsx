/**
 * 当前项目页：项目内容的入口。
 *
 * 一台新装的机器上没有项目，这里就是空的——但不能只显示「无数据」，得把用户送到能建项目
 * 的地方，否则整个应用看上去是坏的。
 */
import { FolderOpenOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Button, Card, Empty, Space, Tabs, Tag, Typography } from 'antd'
import { useNavigate } from 'react-router-dom'

import { ApiError } from '@/api/client'
import { currentProject } from '@/api/projects'
import ArtBibleEditor from '@/components/ArtBibleEditor'
import CharacterTable from '@/components/CharacterTable'
import ProjectConfigForm from '@/components/ProjectConfigForm'

export default function ProjectPage() {
  const navigate = useNavigate()
  const current = useQuery({
    queryKey: ['project-current'],
    queryFn: currentProject,
    // 没选项目时后端就是 404，那是正常状态而不是故障，重试只会拖慢引导页出现
    retry: false,
  })

  const notChosen = current.error instanceof ApiError && current.error.status === 404
  if (notChosen) {
    return (
      <Card>
        <Empty description="还没有选择项目">
          <Space>
            <Button type="primary" onClick={() => navigate('/projects')}>
              去新建一个
            </Button>
            <Button icon={<FolderOpenOutlined />} onClick={() => navigate('/projects')}>
              导入已有目录
            </Button>
          </Space>
        </Empty>
      </Card>
    )
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card size="small" loading={current.isLoading}>
        <Space direction="vertical" size={2}>
          <Space size={8}>
            <Typography.Title level={5} style={{ margin: 0 }}>
              {current.data?.name ?? '…'}
            </Typography.Title>
            <Tag>{current.data?.code}</Tag>
            {current.data?.missing && <Tag color="error">目录不在</Tag>}
          </Space>
          <Typography.Text type="secondary" copyable={{ text: current.data?.dir_path ?? '' }}>
            {current.data?.dir_path ?? ''}
          </Typography.Text>
        </Space>
      </Card>
      <Tabs
        items={[
          { key: 'config', label: '项目配置', children: <ProjectConfigForm /> },
          { key: 'art-bible', label: '视觉规范', children: <ArtBibleEditor /> },
          { key: 'characters', label: '人物素材', children: <CharacterTable /> },
        ]}
      />
    </Space>
  )
}
