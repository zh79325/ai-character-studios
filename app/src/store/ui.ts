/**
 * 只放「跨页面要活着」的那点状态。
 *
 * 服务端数据一律交给 TanStack Query，不往这里搬——那是缓存，不是应用状态。
 * 这里存的是后端进程日志：它由主进程通过 IPC 推来，离开日志页也不能丢。
 */
import { create } from 'zustand'

/** 攒太多会拖慢渲染，超了就丢最旧的。 */
const MAX_LINES = 1000

interface UiState {
  backendLogs: string[]
  logsAttached: boolean
  appendBackendLog: (line: string) => void
  primeBackendLogs: (lines: string[]) => void
  clearBackendLogs: () => void
  markLogsAttached: () => void
}

export const useUiStore = create<UiState>((set) => ({
  backendLogs: [],
  logsAttached: false,
  appendBackendLog: (line) => set((state) => ({ backendLogs: trim([...state.backendLogs, line]) })),
  primeBackendLogs: (lines) => set({ backendLogs: trim(lines) }),
  clearBackendLogs: () => set({ backendLogs: [] }),
  markLogsAttached: () => set({ logsAttached: true }),
}))

function trim(lines: string[]): string[] {
  return lines.length > MAX_LINES ? lines.slice(lines.length - MAX_LINES) : lines
}
