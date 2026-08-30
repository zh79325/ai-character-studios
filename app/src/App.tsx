import { ApiOutlined, DashboardOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Alert, Layout, Menu, Typography } from 'antd'
import type { MenuProps } from 'antd'
import { useEffect } from 'react'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'

import { health } from '@/api/config'
import { useProjectMenu } from '@/components/ProjectMenu'
import AgentsPage from '@/pages/AgentsPage'
import CharacterPage from '@/pages/CharacterPage'
import LogsPage from '@/pages/LogsPage'
import ProjectPage from '@/pages/ProjectPage'
import ProjectsPage from '@/pages/ProjectsPage'
import ProvidersPage from '@/pages/ProvidersPage'
import UsagePage from '@/pages/UsagePage'
import { useUiStore } from '@/store/ui'

/**
 * 顶部导航里除了「项目」那一栏（它带动态子菜单，在 useProjectMenu 里）的固定部分。
 *
 * 角色工作台不进导航：得先有个角色才打开得开，入口在人物素材表里。
 */
const CONFIG_NAV: MenuProps['items'] = [
  {
    key: 'ai',
    icon: <ApiOutlined />,
    label: 'AI 配置',
    children: [
      { key: '/providers', label: 'Provider 配置' },
      { key: '/agents', label: 'Agent 配置' },
    ],
  },
  {
    key: 'system',
    icon: <DashboardOutlined />,
    label: '系统状态',
    children: [
      { key: '/usage', label: '额度看板' },
      { key: '/logs', label: '运行日志' },
    ],
  },
]

export default function App() {
  useBackendLogBridge()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const backend = useQuery({ queryKey: ['health'], queryFn: health, retry: 3, retryDelay: 800 })
  const projectMenu = useProjectMenu()

  return (
    <Layout style={{ height: '100%' }}>
      <Layout.Header
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 24,
          paddingInline: 20,
          background: '#fff',
          borderBottom: '1px solid #f0f0f0',
        }}
      >
        <Typography.Text
          strong
          style={{ fontSize: 16, cursor: 'pointer', flex: '0 0 auto' }}
          title="回项目管理"
          onClick={() => navigate('/projects')}
        >
          AI 素材工坊
        </Typography.Text>
        {/* minWidth 0 让 Menu 能被压缩成 more 折叠，不然窗口窄了菜单会溢出去 */}
        <Menu
          mode="horizontal"
          selectedKeys={[pathname]}
          items={[projectMenu.item, ...(CONFIG_NAV ?? [])]}
          onClick={({ key }) => {
            // 项目菜单的项不是路由，它自己消掉；剩下的 key 就是路径
            if (projectMenu.handle(key)) return
            navigate(key)
          }}
          style={{ flex: 1, minWidth: 0, borderBottom: 'none' }}
        />
      </Layout.Header>
      <Layout.Content style={{ padding: 24, overflowY: 'auto' }}>
        {backend.isError && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message="后端没连上"
            description={
              <>
                {backend.error instanceof Error ? backend.error.message : '原因未知'}
                <br />
                去「系统状态 → 运行日志」看后端进程输出。
              </>
            }
          />
        )}
        <Routes>
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/project" element={<ProjectPage />} />
          {/* 角色工作台不进导航：它得先有个角色才打开得开，入口在人物素材表里 */}
          <Route path="/characters/:id" element={<CharacterPage />} />
          <Route path="/providers" element={<ProvidersPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/usage" element={<UsagePage />} />
          <Route path="/logs" element={<LogsPage />} />
          {/* 进来先落在项目管理：选项目是干任何事的第一步 */}
          <Route path="*" element={<Navigate to="/projects" replace />} />
        </Routes>
      </Layout.Content>
      {projectMenu.dialogs}
    </Layout>
  )
}

/**
 * 后端日志的订阅只挂一次：它得在整个应用生命周期里活着，
 * 挂在日志页上的话，离开页面就漏掉那段输出了。
 */
function useBackendLogBridge(): void {
  const { primeBackendLogs, appendBackendLog, logsAttached, markLogsAttached } = useUiStore()

  useEffect(() => {
    if (logsAttached) return
    const bridge = window.atelier
    if (!bridge) return
    markLogsAttached()
    void bridge.logBacklog().then(primeBackendLogs)
    return bridge.onBackendLog(appendBackendLog)
  }, [logsAttached, markLogsAttached, primeBackendLogs, appendBackendLog])
}
