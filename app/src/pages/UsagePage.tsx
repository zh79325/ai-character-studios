/**
 * 额度看板：每个「provider × 模型」在各计量口径下用了多少、还剩多少、这数是谁给的。
 *
 * 默认读本地镜像（快）；勾上「向远程对账」才去问预占服务（慢但准）。
 */
import { ReloadOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Checkbox, Space, Typography } from 'antd'
import { useState } from 'react'

import { clearBreaker, resetUsage, usageBoard } from '@/api/providers'
import UsageTable from '@/components/UsageTable'

export default function UsagePage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [refresh, setRefresh] = useState(false)

  const board = useQuery({
    queryKey: ['usage', refresh],
    queryFn: () => usageBoard(refresh),
    // 看板要跟得上任务进度，但别把远程对账也拖进定时轮询
    refetchInterval: refresh ? false : 10_000,
  })

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['usage'] })

  const unbreak = useMutation({
    mutationFn: (args: { code: string; modelPk: number }) => clearBreaker(args.code, args.modelPk),
    onSuccess: () => {
      message.success('熔断已放行')
      invalidate()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const clearUsage = useMutation({
    mutationFn: (args: { code: string; modelPk: number; limitKind?: string }) =>
      resetUsage(args.code, args.modelPk, args.limitKind),
    onSuccess: (res) => {
      message.success(`清掉 ${res.cleared} 条本地用量，下次调用重新对账`)
      invalidate()
    },
    onError: (err: Error) => message.error(err.message),
  })

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Space style={{ justifyContent: 'space-between', width: '100%' }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          额度看板
        </Typography.Title>
        <Space>
          <Checkbox checked={refresh} onChange={(e) => setRefresh(e.target.checked)}>
            向远程用量服务对账
          </Checkbox>
          <Button
            icon={<ReloadOutlined />}
            loading={board.isFetching}
            onClick={() => void board.refetch()}
          >
            刷新
          </Button>
        </Space>
      </Space>

      <UsageTable
        board={board.data}
        loading={board.isLoading}
        onClearBreaker={(code, modelPk) => unbreak.mutate({ code, modelPk })}
        onResetUsage={(code, modelPk, limitKind) => clearUsage.mutate({ code, modelPk, limitKind })}
      />
    </Space>
  )
}
