import { describe, expect, it } from 'vitest'

import { parsePortLine, PORT_LINE_PREFIX } from './backend'

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
