import {
  ApiOutlined,
  DashboardOutlined,
  FolderOutlined,
  PictureOutlined,
  ProfileOutlined,
} from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { Alert, Layout, Menu, Typography } from 'antd'
import { useEffect } from 'react'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'

import { health } from '@/api/config'
import ProjectSwitcher from '@/components/ProjectSwitcher'
import LogsPage from '@/pages/LogsPage'
import ProjectPage from '@/pages/ProjectPage'
import ProjectsPage from '@/pages/ProjectsPage'
import ProvidersPage from '@/pages/ProvidersPage'
import UsagePage from '@/pages/UsagePage'
import { useUiStore } from '@/store/ui'

const NAV = [
  { key: '/project', icon: <PictureOutlined />, label: '当前项目' },
  { key: '/projects', icon: <FolderOutlined />, label: '项目管理' },
  { key: '/providers', icon: <ApiOutlined />, label: '服务商设置' },
  { key: '/usage', icon: <DashboardOutlined />, label: '额度看板' },
  { key: '/logs', icon: <ProfileOutlined />, label: '运行日志' },
]

export default function App() {
  useBackendLogBridge()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const backend = useQuery({ queryKey: ['health'], queryFn: health, retry: 3, retryDelay: 800 })

  return (
    <Layout style={{ height: '100%' }}>
      <Layout.Sider width={200} theme="light">
        <div style={{ padding: '20px 16px 12px' }}>
          <Typography.Text strong style={{ fontSize: 16 }}>
            AI 素材工坊
          </Typography.Text>
        </div>
        <div style={{ padding: '0 16px 12px' }}>
          <ProjectSwitcher />
        </div>
        <Menu
          mode="inline"
          selectedKeys={[pathname]}
          items={NAV}
          onClick={({ key }) => navigate(key)}
        />
      </Layout.Sider>
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
                去「运行日志」页看后端进程输出。
              </>
            }
          />
        )}
        <Routes>
          <Route path="/project" element={<ProjectPage />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/providers" element={<ProvidersPage />} />
          <Route path="/usage" element={<UsagePage />} />
          <Route path="/logs" element={<LogsPage />} />
          {/* 进来先落在当前项目；没项目时那一页自己会引导去新建 */}
          <Route path="*" element={<Navigate to="/project" replace />} />
        </Routes>
      </Layout.Content>
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
