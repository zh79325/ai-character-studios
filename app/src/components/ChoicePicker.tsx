/**
 * 待选项面板。
 *
 * Agent 一次给完建议，剩下要用户拍板的几处摆在这儿点。以前这些分歧写在正文里（「你倾向 A
 * 还是 B」），用户得把选项文字手抄回输入框——抄错一个字，模型下一轮就照错的那个往下写。
 *
 * 点完拼出来的那句话用的是 Agent 自己给的字面值，不写「选 B」：指代要靠模型回头数选项，
 * 而它数错的时候没人看得出来。
 *
 * 没点的项不拼进去，只在末尾点名一句「按你的推荐来」。把没点的也按推荐值写成用户的选择，
 * 等于替用户认了几个他没看的结论。
 */
import { Button, Card, Radio, Space, Typography } from 'antd'
import { useState } from 'react'

import type { ChoiceGroup } from '@/types/api'

interface Props {
  groups: ChoiceGroup[]
  /** 会话冻结或这一轮还没回完时不让点。 */
  disabled?: boolean
  /** 把拼好的那句话发出去。 */
  onSubmit: (text: string) => void
}

/** 一批选项的内容签名：详情每次刷新都是新数组，认内容才知道是不是换了一批。 */
function signatureOf(groups: ChoiceGroup[]): string {
  return groups.map((one) => `${one.item}=${one.options.join('|')}`).join('\n')
}

function defaultsOf(groups: ChoiceGroup[]): Record<string, string> {
  const picked: Record<string, string> = {}
  for (const one of groups) {
    if (one.recommended !== '') picked[one.item] = one.recommended
  }
  return picked
}

function compose(groups: ChoiceGroup[], picked: Record<string, string>): string {
  const lines = groups
    .filter((one) => picked[one.item] !== undefined)
    .map((one) => `- ${one.item}: ${picked[one.item]}`)
  const rest = groups.filter((one) => picked[one.item] === undefined).map((one) => one.item)
  const tail = rest.length > 0 ? `\n剩下的（${rest.join('、')}）按你的推荐来。` : ''
  return `这几项我定了：\n${lines.join('\n')}${tail}`
}

export default function ChoicePicker({ groups, disabled = false, onSubmit }: Props) {
  const signature = signatureOf(groups)
  const [picked, setPicked] = useState<Record<string, string>>(() => defaultsOf(groups))
  const [seen, setSeen] = useState(signature)

  // 换了一批选项就重新预选：上一轮的选择留着会让用户以为新的项也已经定了
  if (seen !== signature) {
    setSeen(signature)
    setPicked(defaultsOf(groups))
  }

  if (groups.length === 0) return null

  const count = groups.filter((one) => picked[one.item] !== undefined).length

  return (
    <Card
      size="small"
      title={<span style={{ fontSize: 13 }}>这几项等你拍板</span>}
      styles={{ body: { padding: 12 } }}
    >
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        {groups.map((one) => (
          <Space key={one.item} direction="vertical" size={4} style={{ width: '100%' }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {one.item}
            </Typography.Text>
            <Radio.Group
              size="small"
              optionType="button"
              disabled={disabled}
              value={picked[one.item]}
              style={{ display: 'flex', flexWrap: 'wrap', rowGap: 6 }}
              onChange={(event) =>
                setPicked((prev) => ({ ...prev, [one.item]: event.target.value as string }))
              }
              options={one.options.map((option) => ({
                value: option,
                label: option === one.recommended ? `${option}（推荐）` : option,
              }))}
            />
          </Space>
        ))}
        <Space size={8} wrap>
          <Button
            type="primary"
            size="small"
            disabled={disabled || count === 0}
            onClick={() => onSubmit(compose(groups, picked))}
          >
            就按这些
          </Button>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {count === groups.length
              ? '发出去之后设计师会照这些写草稿'
              : `已选 ${count}/${groups.length}，没选的按它的推荐走`}
          </Typography.Text>
        </Space>
      </Space>
    </Card>
  )
}
