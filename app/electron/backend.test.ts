import { describe, expect, it } from 'vitest'

import { externalPort, parsePortLine, PORT_LINE_PREFIX, stopBackend } from './backend'

describe('parsePortLine', () => {
  it('认出端口行', () => {
    expect(parsePortLine(`${PORT_LINE_PREFIX}62066`)).toBe(62066)
  })

  it('容忍行尾的换行与空格', () => {
    expect(parsePortLine(`${PORT_LINE_PREFIX}8000  \r`)).toBe(8000)
  })

  it('普通日志行不是端口行', () => {
    expect(parsePortLine('INFO:     Uvicorn running on http://127.0.0.1:62066')).toBeNull()
    expect(parsePortLine('')).toBeNull()
  })

  it('前缀对但数字不像端口的照样拒掉', () => {
    expect(parsePortLine(`${PORT_LINE_PREFIX}abc`)).toBeNull()
    expect(parsePortLine(`${PORT_LINE_PREFIX}0`)).toBeNull()
    expect(parsePortLine(`${PORT_LINE_PREFIX}70000`)).toBeNull()
    expect(parsePortLine(`${PORT_LINE_PREFIX}62066.5`)).toBeNull()
  })
})

describe('externalPort', () => {
  it('配了就认，没配或不像端口就当没配', () => {
    expect(externalPort({ ATELIER_BACKEND_PORT: '8765' })).toBe(8765)
    expect(externalPort({})).toBeNull()
    expect(externalPort({ ATELIER_BACKEND_PORT: '' })).toBeNull()
    expect(externalPort({ ATELIER_BACKEND_PORT: 'abc' })).toBeNull()
    expect(externalPort({ ATELIER_BACKEND_PORT: '0' })).toBeNull()
  })
})

describe('stopBackend', () => {
  it('外部后端（process 为 null）不去杀', () => {
    expect(() => stopBackend({ port: 8765, process: null })).not.toThrow()
  })
})
