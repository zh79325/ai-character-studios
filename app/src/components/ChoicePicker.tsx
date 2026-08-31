/**
 * 待选项面板。
 *
 * Agent 一次给完建议，剩下要用户拍板的几处摆在这儿点，一轮最多四项（后端截的）。以前这些
 * 分歧写在正文里（「你倾向 A 还是 B」），用户得把选项文字手抄回输入框——抄错一个字，模型
 * 下一轮就照错的那个往下写。
 *
 * 每项都留了「其他」与一句补充：列出来的几个选项覆盖不到用户真正想要的时候，逼他在这几个里
 * 挑一个等于替他改了需求。自填值与补充都原样拼进那句话，Agent 下一轮照着它对。
 *
 * 点完拼出来的那句话用的是 Agent 自己给的字面值，不写「选 B」：指代要靠模型回头数选项，
 * 而它数错的时候没人看得出来。
 *
 * 没点的项不拼进去，只在末尾点名一句「按你的推荐来」。把没点的也按推荐值写成用户的选择，
 * 等于替用户认了几个他没看的结论。
 *
 * 拍完就不再是表单：点过「就按这些」之后这张卡片翻成定下来的那几行结果。继续摆着可点的单选框，
 * 用户改一下以为改得掉，而那句话已经发出去了。
 */
import { Button, Card, Input, Radio, Space, Tag, Typography } from 'antd'
import { useState } from 'react'

import type { ChoiceGroup } from '@/types/api'

/** 「其他」这一档的值。选项文字里不可能出现控制字符，拿它当哨兵不会跟真选项撞上。 */
const CUSTOM = '\u0000custom'

interface Props {
  groups: ChoiceGroup[]
  /** 这一轮还没回完时不让点。 */
  disabled?: boolean
  /** 把拼好的那句话发出去。 */
  onSubmit: (text: string) => void
}

type Table = Record<string, string>

/** 拍完那一下定成了什么。 */
interface Answer {
  /** 定下的那几项，按面板上的顺序。 */
  settled: { item: string; value: string; note: string }[]
  /** 没点的那几项：那些是按设计师的推荐走的。 */
  rest: string[]
}

/** 一批选项的内容签名：详情每次刷新都是新数组，认内容才知道是不是换了一批。 */
function signatureOf(groups: ChoiceGroup[]): string {
  return groups.map((one) => `${one.item}=${one.options.join('|')}`).join('\n')
}

function defaultsOf(groups: ChoiceGroup[]): Table {
  const picked: Table = {}
  for (const one of groups) {
    if (one.recommended !== '') picked[one.item] = one.recommended
  }
  return picked
}

export default function ChoicePicker({ groups, disabled = false, onSubmit }: Props) {
  const signature = signatureOf(groups)
  const [picked, setPicked] = useState<Table>(() => defaultsOf(groups))
  const [custom, setCustom] = useState<Table>({})
  const [note, setNote] = useState<Table>({})
  const [answer, setAnswer] = useState<Answer | null>(null)
  const [seen, setSeen] = useState(signature)

  // 换了一批选项就重新预选：上一轮的选择与补充留着会让用户以为新的项也已经定了
  if (seen !== signature) {
    setSeen(signature)
    setPicked(defaultsOf(groups))
    setCustom({})
    setNote({})
    setAnswer(null)
  }

  if (groups.length === 0) return null

  /** 这一项当下定成了什么。选了「其他」却没写字就算还没定。 */
  const valueOf = (item: string): string => {
    const chosen = picked[item]
    if (chosen === undefined) return ''
    return chosen === CUSTOM ? (custom[item] ?? '').trim() : chosen
  }

  const settled = groups.filter((one) => valueOf(one.item) !== '')

  const compose = (): string => {
    const lines = settled.map((one) => {
      const extra = (note[one.item] ?? '').trim()
      return `- ${one.item}: ${valueOf(one.item)}${extra === '' ? '' : `（补充：${extra}）`}`
    })
    const rest = groups.filter((one) => valueOf(one.item) === '').map((one) => one.item)
    const tail = rest.length > 0 ? `\n剩下的（${rest.join('、')}）按你的推荐来。` : ''
    return `这几项我定了：\n${lines.join('\n')}${tail}`
  }

  const submit = () => {
    onSubmit(compose())
    setAnswer({
      settled: settled.map((one) => ({
        item: one.item,
        value: valueOf(one.item),
        note: (note[one.item] ?? '').trim(),
      })),
      rest: groups.filter((one) => valueOf(one.item) === '').map((one) => one.item),
    })
  }

  if (answer !== null) return <Answered answer={answer} />

  return (
    <Card
      size="small"
      title={<span style={{ fontSize: 13 }}>这几项等你拍板</span>}
      styles={{ body: { padding: 12 } }}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
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
              options={[
                ...one.options.map((option) => ({
                  value: option,
                  label: option === one.recommended ? `${option}（推荐）` : option,
                })),
                { value: CUSTOM, label: '其他' },
              ]}
            />
            {picked[one.item] === CUSTOM && (
              <Input
                size="small"
                disabled={disabled}
                value={custom[one.item] ?? ''}
                placeholder="你想要的是什么样，写清楚一点"
                onChange={(event) =>
                  setCustom((prev) => ({ ...prev, [one.item]: event.target.value }))
                }
              />
            )}
            <Input
              size="small"
              disabled={disabled}
              value={note[one.item] ?? ''}
              placeholder="补充一句（可选）"
              onChange={(event) => setNote((prev) => ({ ...prev, [one.item]: event.target.value }))}
            />
          </Space>
        ))}
        <Space size={8} wrap>
          <Button
            type="primary"
            size="small"
            disabled={disabled || settled.length === 0}
            onClick={submit}
          >
            就按这些
          </Button>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {settled.length === groups.length
              ? '发出去之后设计师会照这些接着往下定'
              : `已定 ${settled.length}/${groups.length}，没定的按它的推荐走`}
          </Typography.Text>
        </Space>
      </Space>
    </Card>
  )
}

/** 拍完之后这张卡片的样子：只报定下来的结果，不再给控件。 */
function Answered({ answer }: { answer: Answer }) {
  return (
    <Card
      size="small"
      title={<span style={{ fontSize: 13 }}>这几项已经拍了</span>}
      styles={{ body: { padding: 12 } }}
    >
      <Space direction="vertical" size={6} style={{ width: '100%' }}>
        {answer.settled.map((one) => (
          <Space key={one.item} size={6} wrap>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {one.item}
            </Typography.Text>
            <Tag color="blue">{one.value}</Tag>
            {one.note !== '' && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                补充：{one.note}
              </Typography.Text>
            )}
          </Space>
        ))}
        {answer.rest.length > 0 && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            剩下的（{answer.rest.join('、')}）按设计师的推荐走
          </Typography.Text>
        )}
      </Space>
    </Card>
  )
}
