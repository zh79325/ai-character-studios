import { resolve } from 'node:path'

import { defineConfig } from 'vitest/config'

// electron-vite 的配置文件 vitest 认不出来，单独给一份。
// 单元测试只覆盖纯逻辑（端口行解析、URL 拼装、数据整形），不渲染组件，所以环境用 node。
export default defineConfig({
  resolve: {
    alias: { '@': resolve(import.meta.dirname, 'src') },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts', 'electron/**/*.test.ts'],
  },
})
