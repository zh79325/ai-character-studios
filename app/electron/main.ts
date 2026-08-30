/**
 * Electron 主进程：先把后端拉起来拿到端口，再开窗口。
 *
 * 顺序不能反——渲染层第一件事就是问端口，窗口先开只会让它拿到 null。
 */
import { resolve } from 'node:path'

import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron'

import { resolveServerDir, startBackend, stopBackend, type Backend } from './backend'

/** 后端启动与运行日志都先攒在这，渲染层订阅时能补上开窗之前的那段。 */
const LOG_BACKLOG = 500

let backend: Backend | null = null
let startupError: string | null = null
const logs: string[] = []

function pushLog(line: string): void {
  logs.push(line)
  if (logs.length > LOG_BACKLOG) logs.shift()
  for (const win of BrowserWindow.getAllWindows()) {
    win.webContents.send('atelier:backend-log', line)
  }
}

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1080,
    minHeight: 720,
    show: false,
    title: 'AI 素材工坊',
    webPreferences: {
      // electron-vite 按入口文件名出包：electron/preload.ts → out/preload/preload.mjs
      preload: resolve(import.meta.dirname, '../preload/preload.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })

  win.once('ready-to-show', () => win.show())

  // 外链交给系统浏览器，不在应用窗口里开
  win.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url)
    return { action: 'deny' }
  })

  const devServer = process.env.ELECTRON_RENDERER_URL
  if (devServer) {
    void win.loadURL(devServer)
  } else {
    void win.loadFile(resolve(import.meta.dirname, '../renderer/index.html'))
  }
  return win
}

function registerIpc(): void {
  // 端口拿不到时返回 null，渲染层据此显示「后端没起来」而不是白屏
  ipcMain.handle('atelier:port', () => backend?.port ?? null)
  ipcMain.handle('atelier:startup-error', () => startupError)
  ipcMain.handle('atelier:log-backlog', () => [...logs])
  ipcMain.handle('atelier:choose-directory', (_event, defaultPath?: string) =>
    chooseDirectory(defaultPath),
  )
}

/**
 * 挑一个项目目录。
 *
 * `createDirectory` 是给「新建到别处」用的：用户往往要先建个新文件夹再选它。挂在当前窗口
 * 上开成 sheet，不然 macOS 上会飘出一个跟应用无关的独立窗口。
 */
async function chooseDirectory(defaultPath?: string): Promise<string | null> {
  const parent = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0]
  const options = {
    title: '选择项目目录',
    buttonLabel: '选这个目录',
    defaultPath: defaultPath || undefined,
    properties: ['openDirectory' as const, 'createDirectory' as const],
  }
  const result = parent
    ? await dialog.showOpenDialog(parent, options)
    : await dialog.showOpenDialog(options)
  return result.canceled ? null : (result.filePaths[0] ?? null)
}

async function boot(): Promise<void> {
  registerIpc()
  const serverDir = resolveServerDir(app.getAppPath(), process.resourcesPath, app.isPackaged)
  try {
    backend = await startBackend({ serverDir, onLog: pushLog })
    pushLog(`[electron] 后端就绪，端口 ${backend.port}`)
  } catch (err) {
    startupError = err instanceof Error ? err.message : String(err)
    pushLog(`[electron] ${startupError}`)
  }
  createWindow()
}

void app.whenReady().then(boot)

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow()
})

// macOS 上关窗不退应用是惯例，但这个工具带着一个后端进程，留着空跑没意义
app.on('window-all-closed', () => app.quit())

app.on('will-quit', () => stopBackend(backend))
