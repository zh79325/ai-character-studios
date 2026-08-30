/**
 * 行级 diff 的行为。
 *
 * 钉的是几个用户一眼能看出错的地方：行号必须对得上（并排渲染时左右两列各按自己那份文件
 * 编号）、同一处改动要先删后加（否则行内视图里新旧两行会颠倒）、完全没改要能识别出来
 * （拿它挡住「白沉淀一次」）。
 */
import { describe, expect, it } from 'vitest'

import { collapseUnchanged, diffLines, diffStat, splitLines } from './diff'

describe('拆行', () => {
  it('空文本是 0 行，不是一行空行', () => {
    expect(splitLines('')).toEqual([])
  })

  it('结尾的换行不算多出一行', () => {
    expect(splitLines('甲\n乙\n')).toEqual(['甲', '乙'])
  })

  it('中间的空行是内容，要留着', () => {
    expect(splitLines('甲\n\n乙')).toEqual(['甲', '', '乙'])
  })
})

describe('比对', () => {
  it('一模一样时每行都带上两边的行号', () => {
    const lines = diffLines('甲\n乙\n', '甲\n乙\n')
    expect(lines.map((line) => line.kind)).toEqual(['same', 'same'])
    expect(lines.map((line) => [line.currentNo, line.draftNo])).toEqual([
      [1, 1],
      [2, 2],
    ])
  })

  it('改一行是先删后加，删的没有草稿行号，加的没有定稿行号', () => {
    const lines = diffLines('甲\n乙\n丙\n', '甲\n乙改\n丙\n')
    expect(lines.map((line) => [line.kind, line.text])).toEqual([
      ['same', '甲'],
      ['removed', '乙'],
      ['added', '乙改'],
      ['same', '丙'],
    ])
    expect(lines[1]!.draftNo).toBeNull()
    expect(lines[2]!.currentNo).toBeNull()
  })

  it('只在中间插入不会把后面的行也算成改动', () => {
    const lines = diffLines('甲\n丙\n', '甲\n乙\n丙\n')
    expect(lines.map((line) => line.kind)).toEqual(['same', 'added', 'same'])
    // 「丙」在两份里的行号不同，各按自己那份数
    expect(lines[2]).toMatchObject({ currentNo: 2, draftNo: 3 })
  })

  it('从空白起步就是整篇新增——第一次立项走的就是这条路', () => {
    const lines = diffLines('', '甲\n乙\n')
    expect(lines.map((line) => line.kind)).toEqual(['added', 'added'])
  })

  it('删空了就是整篇删除', () => {
    expect(diffLines('甲\n乙\n', '').map((line) => line.kind)).toEqual(['removed', 'removed'])
  })

  it('搬动一段会算成一处删加，不会整篇标成改过', () => {
    const lines = diffLines('甲\n乙\n丙\n', '乙\n丙\n甲\n')
    expect(diffStat(lines)).toMatchObject({ added: 1, removed: 1 })
  })
})

describe('统计', () => {
  it('没有增删就是 identical，沉淀它是白跑一趟', () => {
    expect(diffStat(diffLines('甲\n', '甲\n')).identical).toBe(true)
  })

  it('有一处改动就不算 identical', () => {
    const stat = diffStat(diffLines('甲\n', '甲改\n'))
    expect(stat).toEqual({ added: 1, removed: 1, identical: false })
  })
})

describe('折叠未变区', () => {
  const long = Array.from({ length: 20 }, (_, i) => `第${i + 1}行`).join('\n')
  const edited = long.replace('第10行', '第10行改了')

  it('改动附近留出上下文，远处折起来', () => {
    const chunks = collapseUnchanged(diffLines(long, edited), 2)
    expect(chunks.map((chunk) => chunk.kind)).toEqual(['gap', 'lines', 'gap'])
    // 上下各 2 行 + 一删一加
    expect(chunks[1]!.lines).toHaveLength(6)
  })

  it('折起来的段留着行数，好显示「省略 N 行」', () => {
    const chunks = collapseUnchanged(diffLines(long, edited), 2)
    const folded = chunks.filter((chunk) => chunk.kind === 'gap')
    expect(folded.reduce((sum, chunk) => sum + chunk.lines.length, 0)).toBe(15)
  })

  it('通篇没改就整篇折起来——展开也没东西可看', () => {
    const chunks = collapseUnchanged(diffLines(long, long), 3)
    expect(chunks.map((chunk) => chunk.kind)).toEqual(['gap'])
  })

  it('通篇都改就一段都不折', () => {
    const chunks = collapseUnchanged(diffLines('甲\n乙\n', '丙\n丁\n'), 3)
    expect(chunks.every((chunk) => chunk.kind === 'lines')).toBe(true)
  })
})
