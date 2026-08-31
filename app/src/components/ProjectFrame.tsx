/**
 * 项目内页面的外壳：把「有没有打开项目」这件事挡在页面内容前面。
 *
 * 每个项目页都要先回答同样两个问题——没打开项目时去哪、项目还在立项中时能不能干这件事。
 * 各页自己写一遍迟早走形，所以统一在这里出引导页，页面本身只管自己那块内容。
 */
import { FolderOpenOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Breadcrumb, Button, Card, Empty, Space } from 'antd'
import type { ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { ApiError } from '@/api/client'
import { currentProject } from '@/api/projects'

export interface ProjectBreadcrumbItem {
  label: string
  path?: string
}

interface Props {
  /** 真则立项没收口时不放行：素材目录都还没铺，进去只会看到一片空。 */
  requireReady?: boolean
  /** “项目首页”之后的页面层级。 */
  breadcrumb?: ProjectBreadcrumbItem[]
  children: ReactNode
}

/** 当前项目。各页共用一个 key，切项目时一处失效全体跟上。 */
export function useCurrentProject() {
  return useQuery({
    queryKey: ['project-current'],
    queryFn: currentProject,
    // 没选项目时后端就是 404，那是正常状态而不是故障，重试只会拖慢引导页出现
    retry: false,
  })
}

export default function ProjectFrame({ requireReady = false, breadcrumb = [], children }: Props) {
  const navigate = useNavigate()
  const current = useCurrentProject()

  // 后端只把「打开了谁」记在内存里，没打开就是 404——那是正常状态，拿来出引导页
  if (current.error instanceof ApiError && current.error.status === 404) {
    return (
      <Card>
        <Empty description="还没打开项目">
          <Button
            type="primary"
            icon={<FolderOpenOutlined />}
            onClick={() => navigate('/projects')}
          >
            去项目管理
          </Button>
        </Empty>
      </Card>
    )
  }

  const drafting = current.data?.stage === 'drafting'

  const breadcrumbItems = [
    {
      title: breadcrumb.length === 0 ? '项目首页' : <Link to="/project">项目首页</Link>,
    },
    ...breadcrumb.map((item) => ({
      title: item.path ? <Link to={item.path}>{item.label}</Link> : item.label,
    })),
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Breadcrumb items={breadcrumbItems} />
      {requireReady && drafting ? (
        <Card>
          <Empty description="这个项目还在立项中，先把名字与代号定下来">
            <Button type="primary" onClick={() => navigate('/project')}>
              回立项对焦
            </Button>
          </Empty>
        </Card>
      ) : (
        children
      )}
    </Space>
  )
}
