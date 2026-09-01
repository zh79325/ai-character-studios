/**
 * 当前项目的角色列表。表格支持按关键字和所属目录过滤；新建角色时先明确选择目录，再输入
 * 角色名称。覆盖是删旧目录（含素材）重建，后端仍兜底 409。
 *
 * 「扫描目录」是给「用户直接把角色目录拷进来」这条路准备的：只认领带 `.model.json` 的目录；
 * 扫描发现数据库里有但磁盘上没有的角色时，由用户逐条确认删除记录。
 */
import { FolderOutlined, PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  App,
  Button,
  Card,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tree,
  TreeSelect,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { createCharacter, deleteMissingCharacter } from '@/api/characters'
import { createGroup, listCharacters, listGroups, scanProject } from '@/api/projects'
import type { Character, ScanResult } from '@/types/api'

const STAGE_COLORS = ['default', 'blue', 'cyan', 'geekblue', 'purple', 'gold', 'orange', 'green']

const ALL_GROUPS = '*'
const ROOT_GROUP = '/'

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

/** 从 `characters/玩家角色/赤瞳` 里取出分组路径 `玩家角色`；根分组是空串。 */
function groupOf(dirName: string): string {
  return dirName.split('/').slice(1, -1).join('/')
}

/** 目标角色目录：`characters/<分组>/<名字>`。前端据此在本地判定同分组重名。 */
function targetDir(group: string, name: string): string {
  return ['characters', ...(group ? [group] : []), name].join('/')
}

type DirectoryNode = {
  key: string
  value: string
  title: string
  children: DirectoryNode[]
}

/** 将 `玩家角色/主角` 这类路径转换为目录树。 */
function buildDirectoryTree(paths: Iterable<string>): DirectoryNode[] {
  const roots: DirectoryNode[] = []
  const index = new Map<string, DirectoryNode>()
  for (const path of [...paths].sort()) {
    let prefix = ''
    let siblings = roots
    for (const part of path.split('/')) {
      prefix = prefix ? `${prefix}/${part}` : part
      let node = index.get(prefix)
      if (!node) {
        node = { key: prefix, value: prefix, title: part, children: [] }
        index.set(prefix, node)
        siblings.push(node)
      }
      siblings = node.children
    }
  }
  return roots
}

export default function CharacterTable() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [lastScan, setLastScan] = useState<ScanResult | null>(null)
  const [directoryFilter, setDirectoryFilter] = useState(ALL_GROUPS)
  const [keyword, setKeyword] = useState('')
  const [naming, setNaming] = useState(false)
  const [creationGroup, setCreationGroup] = useState<string>()
  const [name, setName] = useState('')
  const [grouping, setGrouping] = useState(false)
  const [managedGroup, setManagedGroup] = useState('')
  const [groupName, setGroupName] = useState('')

  const rows = useQuery({ queryKey: ['characters'], queryFn: () => listCharacters() })
  const groups = useQuery({ queryKey: ['character-groups'], queryFn: listGroups })

  const refreshAll = () => {
    void queryClient.invalidateQueries({ queryKey: ['characters'] })
    void queryClient.invalidateQueries({ queryKey: ['character-groups'] })
  }

  const directoryTreeData = useMemo(() => {
    const paths = new Set<string>(groups.data ?? [])
    for (const row of rows.data ?? []) {
      const path = groupOf(row.dir_name)
      if (path) paths.add(path)
    }
    return [
      { key: ALL_GROUPS, value: ALL_GROUPS, title: '全部目录', children: [] },
      {
        key: ROOT_GROUP,
        value: '',
        title: '根目录',
        children: buildDirectoryTree(paths),
      },
    ]
  }, [groups.data, rows.data])

  const normalizedKeyword = keyword.trim().toLocaleLowerCase()
  const visibleRows = (rows.data ?? []).filter((row) => {
    const matchesDirectory =
      directoryFilter === ALL_GROUPS || groupOf(row.dir_name) === directoryFilter
    const matchesKeyword =
      normalizedKeyword === '' ||
      row.name.toLocaleLowerCase().includes(normalizedKeyword) ||
      row.dir_name.toLocaleLowerCase().includes(normalizedKeyword)
    return matchesDirectory && matchesKeyword
  })
  const targetGroup = directoryFilter === ALL_GROUPS ? '' : directoryFilter
  const chosenGroup = creationGroup ?? ''

  const closeNaming = () => {
    setNaming(false)
    setCreationGroup(undefined)
    setName('')
  }

  const openNaming = () => {
    setCreationGroup(undefined)
    setName('')
    setNaming(true)
  }

  const runCreate = (overwrite: boolean) =>
    createCharacter(name.trim(), chosenGroup, overwrite).then((row) => {
      closeNaming()
      refreshAll()
      // 建完直接进工作台：刚建的角色下一步一定是去聊设定
      navigate(`/characters/${row.id}`)
    })

  const create = useMutation({
    mutationFn: (overwrite: boolean) => runCreate(overwrite),
    onError: (err: Error) => message.error(err.message),
  })

  const submitCharacter = () => {
    const trimmed = name.trim()
    if (creationGroup === undefined || !trimmed) return
    const clash = (rows.data ?? []).some((row) => row.dir_name === targetDir(chosenGroup, trimmed))
    if (clash) {
      Modal.confirm({
        title: `「${trimmed}」在该分组已存在`,
        content: '覆盖会删掉旧角色目录（含已生成的素材）再重建，这一步不可撤销。',
        okText: '覆盖重建',
        okButtonProps: { danger: true },
        cancelText: '算了',
        onOk: () => create.mutateAsync(true),
      })
      return
    }
    create.mutate(false)
  }

  const group = useMutation({
    mutationFn: (path: string) => createGroup(path),
    onSuccess: (nextGroups, path) => {
      setGroupName('')
      setManagedGroup(path)
      queryClient.setQueryData(['character-groups'], nextGroups)
      message.success('分组建好了')
    },
    onError: (err: Error) => message.error(err.message),
  })

  const submitGroup = () => {
    const sub = groupName.trim()
    if (!sub) return
    group.mutate(managedGroup ? `${managedGroup}/${sub}` : sub)
  }

  const scan = useMutation({
    mutationFn: scanProject,
    onSuccess: (result) => {
      setLastScan(result)
      message.success(result.added.length ? `认领了 ${result.added.length} 个` : '没有新素材')
      refreshAll()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const removeMissing = useMutation({
    mutationFn: deleteMissingCharacter,
    onSuccess: (_, characterId) => {
      setLastScan((current) =>
        current
          ? {
              ...current,
              missing: current.missing.filter((item) => item.id !== characterId),
              total: Math.max(0, current.total - 1),
            }
          : null,
      )
      refreshAll()
      message.success('缺失角色记录已删除')
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
    {
      title: '操作',
      key: 'actions',
      width: 110,
      render: (_, row) => (
        <Button type="primary" size="small" onClick={() => navigate(`/characters/${row.id}`)}>
          开始设计
        </Button>
      ),
    },
  ]

  return (
    <Card size="small" title="角色列表">
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Space wrap>
          <Input
            allowClear
            prefix={<SearchOutlined />}
            value={keyword}
            placeholder="按角色名或目录搜索"
            style={{ width: 240 }}
            onChange={(event) => setKeyword(event.target.value)}
          />
          <TreeSelect
            value={directoryFilter}
            treeData={directoryTreeData}
            treeDefaultExpandAll
            showSearch
            treeNodeFilterProp="title"
            style={{ width: 220 }}
            onChange={setDirectoryFilter}
          />
          <Button type="primary" icon={<PlusOutlined />} onClick={openNaming}>
            新建角色
          </Button>
          <Button
            icon={<FolderOutlined />}
            onClick={() => {
              setManagedGroup(targetGroup)
              setGrouping(true)
            }}
          >
            分组管理
          </Button>
          <Button
            icon={<ReloadOutlined />}
            loading={rows.isFetching || groups.isFetching}
            onClick={() => {
              void rows.refetch()
              void groups.refetch()
            }}
          >
            刷新
          </Button>
          <Button icon={<SearchOutlined />} loading={scan.isPending} onClick={() => scan.mutate()}>
            扫描目录
          </Button>
        </Space>
        {lastScan && lastScan.missing.length > 0 && (
          <Alert
            type="warning"
            showIcon
            message="有角色在数据库里，但磁盘上找不到"
            description={
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                {lastScan.missing.map((item) => (
                  <Space key={item.id} wrap>
                    <Typography.Text>
                      {item.name}（{item.dir_name}）
                    </Typography.Text>
                    <Popconfirm
                      title="删除这条角色记录？"
                      description="只删除数据库记录；角色目录恢复后也需要重新扫描认领。"
                      okText="删除"
                      okButtonProps={{ danger: true }}
                      cancelText="取消"
                      onConfirm={() => removeMissing.mutateAsync(item.id)}
                    >
                      <Button
                        danger
                        size="small"
                        loading={removeMissing.isPending && removeMissing.variables === item.id}
                      >
                        删除记录
                      </Button>
                    </Popconfirm>
                  </Space>
                ))}
              </Space>
            }
          />
        )}
        <Table
          rowKey="id"
          size="small"
          loading={rows.isLoading}
          dataSource={visibleRows}
          columns={columns}
          pagination={false}
          locale={{ emptyText: '没有符合筛选条件的角色' }}
        />
      </Space>

      <Modal
        open={naming}
        title="新建角色"
        okText="建吧"
        cancelText="算了"
        okButtonProps={{ disabled: creationGroup === undefined || name.trim() === '' }}
        confirmLoading={create.isPending}
        onCancel={closeNaming}
        onOk={submitCharacter}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            <Typography.Text>1. 选择角色所在目录</Typography.Text>
            <TreeSelect
              value={creationGroup}
              treeData={directoryTreeData.slice(1)}
              treeDefaultExpandAll
              showSearch
              treeNodeFilterProp="title"
              placeholder="请选择目录"
              style={{ width: '100%' }}
              onChange={setCreationGroup}
            />
          </Space>
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            <Typography.Text>2. 输入角色名称</Typography.Text>
            <Input
              disabled={creationGroup === undefined}
              value={name}
              placeholder="例：赤瞳"
              onChange={(event) => setName(event.target.value)}
              onPressEnter={submitCharacter}
            />
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            名字也是目录名，建完不好改——改名会让已经落盘的素材跟库里的记录对不上。
          </Typography.Text>
        </Space>
      </Modal>

      <Modal open={grouping} title="分组管理" footer={null} onCancel={() => setGrouping(false)}>
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Tree
            blockNode
            defaultExpandAll
            selectedKeys={[managedGroup || ROOT_GROUP]}
            treeData={[
              {
                key: ROOT_GROUP,
                title: '根目录',
                children: directoryTreeData[1]?.children ?? [],
              },
            ]}
            onSelect={(keys) => {
              const key = keys[0]
              if (key === undefined) return
              setManagedGroup(key === ROOT_GROUP ? '' : String(key))
            }}
          />
          <Typography.Text>在「{managedGroup || '根目录'}」下新建子分组</Typography.Text>
          <Space.Compact style={{ width: '100%' }}>
            <Input
              value={groupName}
              placeholder="输入分组名称"
              onChange={(event) => setGroupName(event.target.value)}
              onPressEnter={submitGroup}
            />
            <Button
              type="primary"
              icon={<PlusOutlined />}
              loading={group.isPending}
              disabled={groupName.trim() === ''}
              onClick={submitGroup}
            >
              新建
            </Button>
          </Space.Compact>
        </Space>
      </Modal>
    </Card>
  )
}
