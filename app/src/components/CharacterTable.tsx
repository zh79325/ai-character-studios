/**
 * 当前项目的角色素材，按磁盘分组分层展示。
 *
 * 列表来自项目自带的库，切项目天然隔离；分组只是 characters/ 下的文件夹，磁盘是它们的
 * 真相（空分组没有库行可依），所以分组树直接读盘。左边选中分组、右边只列直属该分组的角色。
 *
 * 新建角色/分组都落在「当前选中的分组」下。重名（同分组同名）先弹确认再带 overwrite 覆盖：
 * 覆盖是删旧目录（含素材）重建，后端仍兜底 409。
 *
 * 「扫描目录」是给「用户直接把角色目录拷进来」这条路准备的：只认领带 `.model.json` 的目录，
 * 只报告消失的目录而不删——目录可能只是还没拷过来。
 */
import { FolderAddOutlined, PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Input, Modal, Space, Table, Tag, Tree, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { createCharacter } from '@/api/characters'
import { createGroup, listCharacters, listGroups, scanProject } from '@/api/projects'
import type { Character, ScanResult } from '@/types/api'

const STAGE_COLORS = ['default', 'blue', 'cyan', 'geekblue', 'purple', 'gold', 'orange', 'green']

/** 根分组用一个不会跟真实分组路径撞的哨兵 key（真实分组路径不以 `/` 开头）。 */
const ROOT_KEY = '/'

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

type TreeNode = { key: string; title: string; children: TreeNode[] }

/** 把一批分组路径（含各层）拼成嵌套树，中间层缺失的自动补齐。 */
function buildTree(paths: Iterable<string>): TreeNode[] {
  const roots: TreeNode[] = []
  const index = new Map<string, TreeNode>()
  for (const path of paths) {
    if (!path) continue
    let prefix = ''
    let siblings = roots
    for (const part of path.split('/')) {
      prefix = prefix ? `${prefix}/${part}` : part
      let node = index.get(prefix)
      if (!node) {
        node = { key: prefix, title: part, children: [] }
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
  const [currentGroup, setCurrentGroup] = useState('')
  const [naming, setNaming] = useState(false)
  const [name, setName] = useState('')
  const [grouping, setGrouping] = useState(false)
  const [groupName, setGroupName] = useState('')

  const rows = useQuery({ queryKey: ['characters'], queryFn: () => listCharacters() })
  const groups = useQuery({ queryKey: ['character-groups'], queryFn: listGroups })

  const refreshAll = () => {
    void queryClient.invalidateQueries({ queryKey: ['characters'] })
    void queryClient.invalidateQueries({ queryKey: ['character-groups'] })
  }

  const treeData = useMemo(() => {
    const paths = new Set<string>(groups.data ?? [])
    for (const row of rows.data ?? []) {
      const group = groupOf(row.dir_name)
      if (group) paths.add(group)
    }
    return [{ key: ROOT_KEY, title: '角色（根）', children: buildTree(paths) }]
  }, [groups.data, rows.data])

  const visibleRows = (rows.data ?? []).filter((row) => groupOf(row.dir_name) === currentGroup)

  const runCreate = (overwrite: boolean) =>
    createCharacter(name.trim(), currentGroup, overwrite).then((row) => {
      setNaming(false)
      setName('')
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
    if (!trimmed) return
    const clash = (rows.data ?? []).some((row) => row.dir_name === targetDir(currentGroup, trimmed))
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
    mutationFn: () => {
      const sub = groupName.trim()
      return createGroup(currentGroup ? `${currentGroup}/${sub}` : sub)
    },
    onSuccess: () => {
      setGrouping(false)
      setGroupName('')
      void queryClient.invalidateQueries({ queryKey: ['character-groups'] })
      message.success('分组建好了')
    },
    onError: (err: Error) => message.error(err.message),
  })

  const scan = useMutation({
    mutationFn: scanProject,
    onSuccess: (result) => {
      setLastScan(result)
      message.success(result.added.length ? `认领了 ${result.added.length} 个` : '没有新素材')
      refreshAll()
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

  const groupLabel = currentGroup || '根'

  return (
    <Card
      size="small"
      title="角色素材"
      extra={
        <Space>
          <Button icon={<FolderAddOutlined />} onClick={() => setGrouping(true)}>
            新建分组
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setNaming(true)}>
            新建角色
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
        <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
          <div style={{ width: 220, flexShrink: 0 }}>
            <Tree
              treeData={treeData}
              selectedKeys={[currentGroup || ROOT_KEY]}
              defaultExpandAll
              onSelect={(keys) => {
                const key = keys[0]
                if (key === undefined) return
                setCurrentGroup(key === ROOT_KEY ? '' : String(key))
              }}
            />
          </div>
          <Table
            style={{ flex: 1, minWidth: 0 }}
            rowKey="id"
            size="small"
            loading={rows.isLoading}
            dataSource={visibleRows}
            columns={columns}
            pagination={false}
            locale={{
              emptyText: `分组「${groupLabel}」下还没有角色。点「新建角色」建一个，或把角色目录拷进来再扫描认领`,
            }}
          />
        </div>
      </Space>

      <Modal
        open={naming}
        title={`在分组「${groupLabel}」下新建角色`}
        okText="建吧"
        cancelText="算了"
        okButtonProps={{ disabled: name.trim() === '' }}
        confirmLoading={create.isPending}
        onCancel={() => setNaming(false)}
        onOk={submitCharacter}
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            名字也是目录名，建完不好改——改名会让已经落盘的素材跟库里的记录对不上。
          </Typography.Text>
          <Input
            value={name}
            placeholder="例：赤瞳"
            onChange={(event) => setName(event.target.value)}
            onPressEnter={submitCharacter}
          />
        </Space>
      </Modal>

      <Modal
        open={grouping}
        title={`在分组「${groupLabel}」下新建子分组`}
        okText="建吧"
        cancelText="算了"
        okButtonProps={{ disabled: groupName.trim() === '' }}
        confirmLoading={group.isPending}
        onCancel={() => setGrouping(false)}
        onOk={() => group.mutate()}
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            分组就是 characters/ 下的一个文件夹，用来把角色分门别类（如玩家角色、boss 角色）。
          </Typography.Text>
          <Input
            value={groupName}
            placeholder="例：boss角色"
            onChange={(event) => setGroupName(event.target.value)}
            onPressEnter={() => groupName.trim() !== '' && group.mutate()}
          />
        </Space>
      </Modal>
    </Card>
  )
}
