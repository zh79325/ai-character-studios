/**
 * 当前项目的角色素材。
 *
 * 列表来自项目自带的库，所以切项目天然就隔离了，这里不需要按项目过滤。
 *
 * 「扫描目录」是给「用户直接把素材目录拷进来」这条路准备的：磁盘是素材的真相，库只是索引。
 * 扫描只认领新目录、只报告消失的目录而不删——目录可能只是还没拷过来。
 */
import { PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Input, Modal, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { createCharacter } from '@/api/characters'
import { listCharacters, scanProject } from '@/api/projects'
import type { Character, ScanResult } from '@/types/api'

const STAGE_COLORS = ['default', 'blue', 'cyan', 'geekblue', 'purple', 'gold', 'orange', 'green']

/**
 * 阶段名按 `S0_spec_drafting` 这样的形式排，序号越大越靠后，颜色跟着往后走。
 *
 * 不写死一张状态表：后续工作流会往里加阶段，写死的表漏一个就退回灰色，看着像出错了。
 */
function stageColor(state: string): string {
  const stage = /^S(\d)/.exec(state)
  if (!stage?.[1]) return 'default'
  return STAGE_COLORS[Number(stage[1])] ?? 'green'
}

export default function CharacterTable() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [lastScan, setLastScan] = useState<ScanResult | null>(null)
  const [naming, setNaming] = useState(false)
  const [name, setName] = useState('')
  const rows = useQuery({ queryKey: ['characters'], queryFn: () => listCharacters() })

  const create = useMutation({
    mutationFn: () => createCharacter(name.trim()),
    onSuccess: (row) => {
      setNaming(false)
      setName('')
      void queryClient.invalidateQueries({ queryKey: ['characters'] })
      // 建完直接进工作台：刚建的角色下一步一定是去聊设定
      navigate(`/characters/${row.id}`)
    },
    onError: (err: Error) => message.error(err.message),
  })

  const scan = useMutation({
    mutationFn: scanProject,
    onSuccess: (result) => {
      setLastScan(result)
      message.success(result.added.length ? `认领了 ${result.added.length} 个` : '没有新素材')
      void queryClient.invalidateQueries({ queryKey: ['characters'] })
    },
    onError: (err: Error) => message.error(err.message),
  })

  const columns: ColumnsType<Character> = [
    {
      title: '角色',
      dataIndex: 'name',
      render: (name: string, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Link strong onClick={() => navigate(`/characters/${row.id}`)}>
            {name}
          </Typography.Link>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {row.dir_name}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '阶段',
      dataIndex: 'state',
      width: 140,
      render: (state: string, row) => <Tag color={stageColor(state)}>{row.state_label}</Tag>,
    },
    {
      title: '设定稿',
      dataIndex: 'spec_path',
      ellipsis: true,
      render: (path: string | null) =>
        path ? (
          <Typography.Text code>{path}</Typography.Text>
        ) : (
          <Typography.Text type="secondary">还没有</Typography.Text>
        ),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 180,
      render: (at: string) => at.replace('T', ' ').slice(0, 19),
    },
  ]

  return (
    <Card
      size="small"
      title="角色素材"
      extra={
        <Space>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setNaming(true)}>
            新建角色
          </Button>
          <Button
            icon={<ReloadOutlined />}
            loading={rows.isFetching}
            onClick={() => void rows.refetch()}
          >
            刷新
          </Button>
          <Button icon={<SearchOutlined />} loading={scan.isPending} onClick={() => scan.mutate()}>
            扫描目录
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {lastScan && lastScan.missing.length > 0 && (
          <Alert
            type="warning"
            showIcon
            message="有素材在库里但磁盘上找不到"
            description={`${lastScan.missing.join('、')}。目录可能还没拷过来，平台不会替你删记录。`}
          />
        )}
        <Table
          rowKey="id"
          size="small"
          loading={rows.isLoading}
          dataSource={rows.data ?? []}
          columns={columns}
          pagination={false}
          locale={{
            emptyText:
              '还没有角色。点「新建角色」开一个，或把已有的素材目录拷进项目的 characters/ 下再扫描认领',
          }}
        />
      </Space>

      <Modal
        open={naming}
        title="新建角色"
        okText="建吧"
        cancelText="算了"
        okButtonProps={{ disabled: name.trim() === '' }}
        confirmLoading={create.isPending}
        onCancel={() => setNaming(false)}
        onOk={() => create.mutate()}
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            名字也是目录名，建完不好改——改名会让已经落盘的素材跟库里的记录对不上。
          </Typography.Text>
          <Input
            value={name}
            placeholder="例：赤瞳"
            onChange={(event) => setName(event.target.value)}
            onPressEnter={() => name.trim() !== '' && create.mutate()}
          />
        </Space>
      </Modal>
    </Card>
  )
}
