/**
 * 项目内页面的外壳：用 URL 里的项目代号验证项目存在，再挡住尚未完成立项的页面。
 *
 * 每个项目页都要先回答同样两个问题——URL 指定的项目是否存在、项目还在立项中时能不能干
 * 这件事。各页自己写一遍迟早走形，所以统一在这里出引导页，页面本身只管自己那块内容。
 */
import { FolderOpenOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Breadcrumb, Button, Card, Empty, Space } from 'antd'
import type { ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { ApiError } from '@/api/client'
import { readProject } from '@/api/projects'
import { projectPath, useProjectCode } from '@/lib/projectRoute'

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

/** URL 指定的项目。缓存按项目代号隔离。 */
export function useCurrentProject() {
  const projectCode = useProjectCode()
  return useQuery({
    queryKey: ['project', projectCode],
    queryFn: () => readProject(projectCode),
    retry: false,
  })
}

export default function ProjectFrame({ requireReady = false, breadcrumb = [], children }: Props) {
  const navigate = useNavigate()
  const projectCode = useProjectCode()
  const current = useCurrentProject()

  if (current.error instanceof ApiError && current.error.status === 404) {
    return (
      <Card>
        <Empty description="项目未登记或目录不存在">
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
      title:
        breadcrumb.length === 0 ? '项目首页' : <Link to={projectPath(projectCode)}>项目首页</Link>,
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
            <Button type="primary" onClick={() => navigate(projectPath(projectCode))}>
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
