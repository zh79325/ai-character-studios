import { describe, expect, it } from 'vitest'

import { visibleText } from './message'

describe('visibleText', () => {
  it('剥掉完整 Action 块，只留下正文', () => {
    const text = [
      '这版我按都市调子写的。',
      '',
      '<-------- ACTION-START------->',
      '{',
      '  "action": "ask_user",',
      '  "target_agent": null,',
      '  "reason": "需要确认风格",',
      '  "payload": {}',
      '}',
      '<-------- ACTION-END------->',
    ].join('\n')

    expect(visibleText(text)).toBe('这版我按都市调子写的。')
  })

  it('待选项、命名建议、进度这几块都不进气泡', () => {
    const text = [
      '定了三项，剩下的等你拍。',
      '[待选项]',
      '- 项: 面数预算 / 选项: 8k | 15k / 推荐: 15k',
      '[项目命名建议]',
      '- 名称: 都市西游 / 代号: urban_journey',
      '[对焦进度]',
      '已定: 半写实',
    ].join('\n')

    expect(visibleText(text)).toBe('定了三项，剩下的等你拍。')
  })

  it('流式生成到一半的 Action 也不露出来', () => {
    expect(visibleText('先给一版。\n<-------- ACTION-START------->\n{"action":')).toBe('先给一版。')
    expect(visibleText('先给一版。\n<-------- ACT')).toBe('先给一版。')
  })

  it('块包在引用里也认得出来', () => {
    expect(visibleText('说完了。\n> [项目记忆]\n> preference: 不要粉色')).toBe('说完了。')
  })

  it('整轮全是块就没有正文', () => {
    expect(visibleText('[待选项]\n- 项: 面数预算 / 选项: 8k | 15k')).toBe('')
  })

  it('没有块的时候原样留着', () => {
    expect(visibleText('这几处我想跟你确认一下：色彩要不要压暗？')).toBe(
      '这几处我想跟你确认一下：色彩要不要压暗？',
    )
  })

  it('包在 ``` 里的块不会剩下一条空代码块', () => {
    const text = [
      '这是完整草稿：',
      '',
      '```',
      '[草稿开始: art-bible.md]',
      '# 美术圣经',
      '[草稿结束]',
      '```',
      '',
      '```',
      '[对焦进度]',
      '已定：全部',
      '```',
    ].join('\n')

    expect(visibleText(text)).toBe('这是完整草稿：')
  })

  it('正文里真的代码块不能被剥掉', () => {
    const text = ['改这一行：', '```', 'style: voxel', '```'].join('\n')

    expect(visibleText(text)).toBe(text)
  })
})
