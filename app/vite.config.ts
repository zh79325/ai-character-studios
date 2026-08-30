import { resolve } from 'node:path'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 只跑渲染层：`npm run dev:web` 用它，在浏览器里开，不起 Electron、不起后端。
// 这时 window.atelier 不存在，后端地址由 src/api/client.ts 按 VITE_API_PORT 兜底，
// 所以后端要单独起在那个端口上：uv run atelier-serve --port 8799
//
// electron.vite.config.ts 里的 renderer 段是 Electron 用的，两份各管一边，别合。
export default defineConfig({
  root: import.meta.dirname,
  plugins: [react()],
  resolve: {
    alias: { '@': resolve(import.meta.dirname, 'src') },
  },
  server: {
    port: 5173,
    // 端口被占就报错退出，不偷偷换一个：后端 CORS 白名单写死了 5173
    strictPort: true,
  },
  build: {
    outDir: resolve(import.meta.dirname, 'out/web'),
    emptyOutDir: true,
  },
})
