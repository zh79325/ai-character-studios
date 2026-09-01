/**
 * 项目管理页。
 *
 * 这里管的是「本机认识哪些项目」，不是项目的内容。三个动作对应三种真实处境：新建（从零
 * 开一个）、导入（换机器、外置盘、同事拷来的目录）、扫默认根（用户直接把目录拖进
 * `assets/`）。移出只删本机索引，磁盘上的文件一个不动——那是用户的资产。
 *
 * 顶栏的「项目」菜单干的是同一批事，两边都留着：菜单是干活途中的快捷入口，这页才看得到
 * 目录等索引信息。
 */
import { FolderOpenOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Card, Modal, Popconfirm, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { forgetProject, importProject, listProjects } from '@/api/projects'
import DirectoryPicker from '@/components/DirectoryPicker'
import ProjectBootstrapModal from '@/components/ProjectBootstrapModal'
import { projectPath } from '@/lib/projectRoute'
import type { ProjectList, ProjectSummary } from '@/types/api'

export default function ProjectsPage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [creating, setCreating] = useState(false)
  const [importDir, setImportDir] = useState<string | null>(null)

  const list = useQuery({ queryKey: ['projects'], queryFn: () => listProjects() })

  const adopt = (fresh: ProjectList) => {
    queryClient.setQueryData(['projects'], fresh)
  }

  const enter = (project: ProjectSummary) => {
    queryClient.setQueryData(['project', project.code], project)
    void queryClient.invalidateQueries({ queryKey: ['projects'] })
    navigate(projectPath(project.code))
  }

  const sync = useMutation({
    mutationFn: () => listProjects(true),
    onSuccess: (fresh) => {
      const known = new Set((list.data?.projects ?? []).map((item) => item.code))
      const added = fresh.projects.filter((item) => !known.has(item.code))
      message.success(added.length ? `认领了 ${added.length} 个项目` : '默认目录里没有新项目')
      adopt(fresh)
    },
    onError: (err: Error) => message.error(err.message),
  })

  const doImport = useMutation({
    mutationFn: (dir: string) => importProject(dir),
    onSuccess: (project) => {
      message.success(`已导入 ${project.name}`)
      setImportDir(null)
      enter(project)
    },
    onError: (err: Error) => message.error(err.message),
  })

  const forget = useMutation({
    mutationFn: (code: string) => forgetProject(code),
    onSuccess: () => {
      message.success('已从本机移出，磁盘上的目录还在')
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
    onError: (err: Error) => message.error(err.message),
  })

  const columns: ColumnsType<ProjectSummary> = [
    {
      title: '项目',
      dataIndex: 'name',
      render: (name: string, row) => (
        <Space direction="vertical" size={0}>
          <Space size={6}>
            <Typography.Text strong>{name}</Typography.Text>
            {row.stage === 'drafting' && <Tag color="orange">立项中</Tag>}
            {row.missing && <Tag color="error">目录不在</Tag>}
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {row.code}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '目录',
      dataIndex: 'dir_path',
      ellipsis: true,
      render: (dir: string, row) => (
        <Space size={6}>
          <Typography.Text type={row.missing ? 'danger' : undefined} copyable={{ text: dir }}>
            {dir}
          </Typography.Text>
          {!row.managed && <Tag>外部位置</Tag>}
        </Space>
      ),
    },
    {
      title: '操作',
      width: 180,
      render: (_, row) => (
        <Space size={4}>
          <Button
            size="small"
            type="primary"
            disabled={row.missing}
            onClick={() => navigate(projectPath(row.code))}
          >
            进入
          </Button>
          <Popconfirm
            title={`把 ${row.code} 移出本机？`}
            description="只删本机索引，磁盘上的项目目录一个字节都不动，之后还能再导入回来。"
            okButtonProps={{ danger: true }}
            onConfirm={() => forget.mutate(row.code)}
          >
            <Button size="small" danger>
              移出
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        size="small"
        title="本机项目"
        extra={
          <Space>
            <Button icon={<PlusOutlined />} type="primary" onClick={() => setCreating(true)}>
              新建项目
            </Button>
            <Button icon={<FolderOpenOutlined />} onClick={() => setImportDir('')}>
              导入已有目录
            </Button>
            <Button
              icon={<ReloadOutlined />}
              loading={sync.isPending}
              onClick={() => sync.mutate()}
              title="扫一遍默认项目根，认领手动拷进去的项目"
            >
              扫默认目录
            </Button>
          </Space>
        }
      >
        <Table
          rowKey="code"
          size="small"
          loading={list.isLoading}
          dataSource={list.data?.projects ?? []}
          columns={columns}
          pagination={false}
          locale={{ emptyText: '还没有项目，先新建一个，或者导入一个已有的项目目录' }}
        />
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12 }}>
          默认项目根：{list.data?.default_root ?? '—'}
          。项目也可以放在磁盘任意位置，配置与运行库都在项目目录里，整份拷走换台机器导入即可。
        </Typography.Paragraph>
      </Card>

      <ProjectBootstrapModal
        open={creating}
        defaultRoot={list.data?.default_root ?? ''}
        onClose={() => setCreating(false)}
        onCreated={enter}
      />

      <Modal
        open={importDir !== null}
        title="导入项目"
        okText="导入并进入"
        confirmLoading={doImport.isPending}
        okButtonProps={{ disabled: !importDir?.trim() }}
        onCancel={() => setImportDir(null)}
        onOk={() => importDir?.trim() && doImport.mutate(importDir.trim())}
        destroyOnHidden
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Text type="secondary">
            选中的目录里得有 `project.json`；代号与配置都以那份文件为准。
          </Typography.Text>
          <DirectoryPicker
            value={importDir ?? ''}
            onChange={setImportDir}
            placeholder="/Volumes/外置盘/赤瞳系列"
            defaultPath={list.data?.default_root}
          />
        </Space>
      </Modal>
    </Space>
  )
}
