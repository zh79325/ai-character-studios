import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { HashRouter } from 'react-router-dom'

import App from './App'
import './index.css'

// 打包后走 file:// 协议，BrowserRouter 的 history API 在这上面对不上，只能用 hash
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 本机后端，重试没意义；失败就把原因摆出来让用户去设置页处理
      retry: false,
      refetchOnWindowFocus: false,
      staleTime: 5_000,
    },
  },
})

const root = document.getElementById('root')
if (!root) throw new Error('index.html 里没有 #root')

createRoot(root).render(
  <StrictMode>
    <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#5b21b6' } }}>
      <QueryClientProvider client={queryClient}>
        <AntApp>
          <HashRouter>
            <App />
          </HashRouter>
        </AntApp>
      </QueryClientProvider>
    </ConfigProvider>
  </StrictMode>,
)
