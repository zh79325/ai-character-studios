import { resolve } from 'node:path'

import react from '@vitejs/plugin-react'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'

// 目录布局按开发文档：主进程侧全在 electron/，渲染进程全在 src/。
export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
    build: {
      lib: { entry: resolve(import.meta.dirname, 'electron/main.ts') },
    },
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
    build: {
      lib: { entry: resolve(import.meta.dirname, 'electron/preload.ts') },
    },
  },
  renderer: {
    root: import.meta.dirname,
    plugins: [react()],
    resolve: {
      alias: { '@': resolve(import.meta.dirname, 'src') },
    },
    build: {
      rollupOptions: { input: resolve(import.meta.dirname, 'index.html') },
    },
  },
})
