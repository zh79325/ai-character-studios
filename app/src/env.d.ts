/// <reference types="vite/client" />

import type { AtelierBridge } from './types/bridge'

declare global {
  interface Window {
    /** preload 注入；脱离 Electron 直接开浏览器时是 undefined。 */
    atelier?: AtelierBridge
  }

  interface ImportMetaEnv {
    /** 脱离 Electron 调渲染层时手填后端端口，不填按 8799。 */
    readonly VITE_API_PORT?: string
  }
}
