/**
 * 助手消息的可读正文。
 *
 * Agent 每轮的输出里夹着几个结构块（`[草稿开始: 路径]…[草稿结束]`、`[待选项]`、
 * `[项目命名建议]`、`[对焦进度]`、`[项目记忆]`/`[角色记忆]`），它们各自已经在界面上有位置：
 * 草稿在草稿区、待选项在抽屉、命名建议在立项收口、进度与记忆落库。把原文整段摆进气泡，
 * 用户会在同一屏上把同一件事看两遍——第二遍还是模型内部那套写法。
 *
 * 边界跟后端 `parsing.py` 的 `_BLOCK_END_RE` 对齐：一个块从它的标记行开始，到下一个标记行
 * 或文本结束为止。生成中的半截块（标记行已出、结束行还没出）也一并当块吃掉，不然字一个个
 * 冒出来的时候会先闪一段 `[草稿开始` 再消失。
 */

/** 标记行。行首容忍缩进与 `>`：模型爱把块包在引用里。 */
const MARKER = /^[ \t>]*\[(草稿开始|草稿结束|对焦进度|项目记忆|角色记忆|项目命名建议|待选项)/

/** 剥掉结构块，只留下给人看的那几段话。 */
export function visibleText(text: string): string {
  const kept: string[] = []
  let dropping = false

  for (const line of text.split('\n')) {
    const hit = MARKER.exec(line)
    if (hit !== null) {
      // `[草稿结束]` 是收尾行，它自己被吃掉，后面的正文重新算可见
      dropping = hit[1] !== '草稿结束'
      continue
    }
    if (!dropping) kept.push(line)
  }

  return kept.join('\n').trim()
}
