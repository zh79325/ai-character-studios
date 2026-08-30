/**
 * 运行日志页：上半是选路决策的实时流（SSE），下半是后端进程自己的 stdout/stderr。
 *
 * 选路流是永不结束的连接，所以订阅只在这个页面挂载期间存在，离开就断。
 */
import { ClearOutlined, PauseCircleOutlined, PlayCircleOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Button, Card, Select, Space, Typography } from 'antd'
import { useEffect, useMemo, useRef, useState } from 'react'

import { agents as fetchAgents } from '@/api/config'
import { subscribeRouteLogs } from '@/api/events'
import RouteLogTable from '@/components/RouteLogTable'
import { useUiStore } from '@/store/ui'
import type { RouteLog } from '@/types/api'

/** 面板上留多少条选路记录，多了只会拖慢渲染。 */
const KEEP = 300

export default function LogsPage() {
  const [logs, setLogs] = useState<RouteLog[]>([])
  const [paused, setPaused] = useState(false)
  const [agentCode, setAgentCode] = useState<string | undefined>()
  const agentList = useQuery({
    queryKey: ['agents'],
    queryFn: () => fetchAgents(),
    staleTime: Infinity,
  })

  useEffect(() => {
    if (paused) return
    // agentCode 变了就重连，让后端只推这一个 Agent 的决策，省得前端过滤
    return subscribeRouteLogs({
      agentCode,
      onLog: (log) =>
        setLogs((prev) => {
          const next = [log, ...prev]
          return next.length > KEEP ? next.slice(0, KEEP) : next
        }),
    })
  }, [paused, agentCode])

  const backendLogs = useUiStore((state) => state.backendLogs)
  const clearBackendLogs = useUiStore((state) => state.clearBackendLogs)
  const paneRef = useRef<HTMLDivElement>(null)
  const backendText = useMemo(() => backendLogs.join('\n'), [backendLogs])

  useEffect(() => {
    // 后端日志是追加式的，跟着滚到底才符合看日志的习惯
    const pane = paneRef.current
    if (pane) pane.scrollTop = pane.scrollHeight
  }, [backendText])

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Typography.Title level={4} style={{ margin: 0 }}>
        运行日志
      </Typography.Title>

      <Card
        size="small"
        title="选路决策（实时）"
        extra={
          <Space>
            <Select
              allowClear
              placeholder="按 Agent 过滤"
              style={{ width: 220 }}
              value={agentCode}
              onChange={setAgentCode}
              options={(agentList.data ?? []).map((agent) => ({
                value: agent.agent_code,
                label: agent.agent_code,
              }))}
            />
            <Button
              icon={paused ? <PlayCircleOutlined /> : <PauseCircleOutlined />}
              onClick={() => setPaused((prev) => !prev)}
            >
              {paused ? '继续' : '暂停'}
            </Button>
            <Button icon={<ClearOutlined />} onClick={() => setLogs([])}>
              清屏
            </Button>
          </Space>
        }
      >
        <RouteLogTable logs={logs} />
      </Card>

      <Card
        size="small"
        title="后端进程输出"
        extra={
          <Button icon={<ClearOutlined />} onClick={clearBackendLogs}>
            清屏
          </Button>
        }
      >
        <div className="log-pane" ref={paneRef}>
          {backendText || '（还没有输出。不在 Electron 里跑时这里一直是空的。）'}
        </div>
      </Card>
    </Space>
  )
}
