/**
 * 行级 diff。
 *
 * 后端只给两份全文，算法与展示都在前端——这样并排/行内/折叠未变区都能自己定。这里用
 * 最朴素的 LCS：定稿是人写的规范文档，几百行的量级，O(n·m) 完全够用，换成 Myers 只是
 * 把代码变难读。
 */

export type DiffKind = 'same' | 'added' | 'removed'

export interface DiffLine {
  kind: DiffKind
  text: string
  /** 在当前定稿里的行号，新增行没有。 */
  currentNo: number | null
  /** 在草稿里的行号，删除行没有。 */
  draftNo: number | null
}

/** 空文本按 0 行算，不然会凭空多出一行「空行」。 */
export function splitLines(text: string): string[] {
  if (text === '') return []
  const lines = text.split('\n')
  // 文件通常以换行结尾，那个尾部空串不是一行内容
  if (lines[lines.length - 1] === '') lines.pop()
  return lines
}

/** 最长公共子序列的长度表。 */
function lcsTable(left: string[], right: string[]): number[][] {
  const table: number[][] = Array.from({ length: left.length + 1 }, () =>
    new Array<number>(right.length + 1).fill(0),
  )
  for (let i = left.length - 1; i >= 0; i -= 1) {
    for (let j = right.length - 1; j >= 0; j -= 1) {
      table[i]![j] =
        left[i] === right[j]
          ? table[i + 1]![j + 1]! + 1
          : Math.max(table[i + 1]![j]!, table[i]![j + 1]!)
    }
  }
  return table
}

/**
 * 把「当前定稿 → 草稿」的差异摊成一串行。
 *
 * 同一处改动会出现一条 removed 加一条 added，顺序是先删后加——并排渲染时两边各取自己
 * 那半，行内渲染时上下相邻，都不用再排。
 */
export function diffLines(current: string, draft: string): DiffLine[] {
  const left = splitLines(current)
  const right = splitLines(draft)
  const table = lcsTable(left, right)
  const out: DiffLine[] = []
  let i = 0
  let j = 0

  while (i < left.length && j < right.length) {
    if (left[i] === right[j]) {
      out.push({ kind: 'same', text: left[i]!, currentNo: i + 1, draftNo: j + 1 })
      i += 1
      j += 1
    } else if (table[i + 1]![j]! >= table[i]![j + 1]!) {
      out.push({ kind: 'removed', text: left[i]!, currentNo: i + 1, draftNo: null })
      i += 1
    } else {
      out.push({ kind: 'added', text: right[j]!, currentNo: null, draftNo: j + 1 })
      j += 1
    }
  }
  while (i < left.length) {
    out.push({ kind: 'removed', text: left[i]!, currentNo: i + 1, draftNo: null })
    i += 1
  }
  while (j < right.length) {
    out.push({ kind: 'added', text: right[j]!, currentNo: null, draftNo: j + 1 })
    j += 1
  }
  return out
}

export interface DiffStat {
  added: number
  removed: number
  /** 一个字都没改：草稿与定稿完全一样，沉淀下去也是白跑一趟。 */
  identical: boolean
}

export function diffStat(lines: DiffLine[]): DiffStat {
  const added = lines.filter((line) => line.kind === 'added').length
  const removed = lines.filter((line) => line.kind === 'removed').length
  return { added, removed, identical: added === 0 && removed === 0 }
}

/**
 * 折叠连续的未变行，只在改动附近留 `context` 行。
 *
 * 返回的是按顺序排好的段：`gap` 段代表折起来的那批，带上行数好显示「省略 N 行」。
 */
export interface DiffChunk {
  kind: 'lines' | 'gap'
  lines: DiffLine[]
}

export function collapseUnchanged(lines: DiffLine[], context = 3): DiffChunk[] {
  const keep = new Array<boolean>(lines.length).fill(false)
  lines.forEach((line, index) => {
    if (line.kind === 'same') return
    for (
      let k = Math.max(0, index - context);
      k <= Math.min(lines.length - 1, index + context);
      k += 1
    ) {
      keep[k] = true
    }
  })

  const chunks: DiffChunk[] = []
  for (const [index, line] of lines.entries()) {
    const kind: DiffChunk['kind'] = keep[index] ? 'lines' : 'gap'
    const last = chunks[chunks.length - 1]
    if (last && last.kind === kind) last.lines.push(line)
    else chunks.push({ kind, lines: [line] })
  }
  return chunks
}
