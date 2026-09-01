import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetBaseUrl } from './client'
import { subscribeRouteLogs, subscribeTask } from './events'

class FakeEventSource {
  static made: FakeEventSource[] = []
  closed = false

  constructor(readonly url: string) {
    FakeEventSource.made.push(this)
  }

  addEventListener(): void {}

  close(): void {
    this.closed = true
  }
}

const settled = () => new Promise((resolve) => setTimeout(resolve, 0))

beforeEach(() => {
  FakeEventSource.made = []
  ;(globalThis as { window?: unknown }).window = {
    atelier: {
      port: () => Promise.resolve(62066),
      startupError: () => Promise.resolve(null),
      logBacklog: () => Promise.resolve([]),
      onBackendLog: () => () => undefined,
      chooseDirectory: () => Promise.resolve(null),
    },
  }
  vi.stubGlobal('EventSource', FakeEventSource)
})

afterEach(() => {
  resetBaseUrl()
  vi.unstubAllGlobals()
  delete (globalThis as { window?: unknown }).window
})

describe('事件订阅地址', () => {
  it('任务流显式包含编码后的项目代号与任务 id', async () => {
    subscribeTask({
      projectCode: 'p one',
      taskId: 'task/1',
      afterSeq: 7,
      onEvent: () => undefined,
    })

    await settled()

    expect(FakeEventSource.made[0]?.url).toBe(
      'http://127.0.0.1:62066/api/projects/p%20one/events/task%2F1?after_seq=7',
    )
  })

  it('全局路由日志不携带项目上下文', async () => {
    subscribeRouteLogs({ agentCode: 'designer', afterId: 3, onLog: () => undefined })

    await settled()

    expect(FakeEventSource.made[0]?.url).toBe(
      'http://127.0.0.1:62066/api/events/route-logs?agent_code=designer&after_id=3',
    )
  })
})
