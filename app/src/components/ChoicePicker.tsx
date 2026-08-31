/**
 * 待选项抽屉。
 *
 * Agent 一次给完建议，剩下要用户拍板的几处摆在这儿点，一轮最多四项（后端截的）。以前这些
 * 分歧写在正文里（「你倾向 A 还是 B」），用户得把选项文字手抄回输入框——抄错一个字，模型
 * 下一轮就照错的那个往下写。
 *
 * 摆成从底部抽出来的抽屉，而不是夹在输入框上面的卡片：选项文字是模型写的整句，四项一起铺在
 * 消息区与输入框中间会把两头都挤没了。抽屉盖住它们，按钮固定在底，选项再长也只是抽屉里滚。
 *
 * 一项一行、一个选项一行：选项之间长短差得远，横着排会折成参差的几行，看着像分组错位。
 *
 * 每项都留了「其他」：列出来的几个覆盖不到用户真正想要的时候，逼他在这几个里挑一个等于替他改
 * 了需求。选了「其他」就只写自填值，不再多给一个补充框——同一件事两个框，用户得猜该写哪个。
 *
 * 点完拼出来的那句话用的是 Agent 自己给的字面值，不写「选 B」：指代要靠模型回头数选项，
 * 而它数错的时候没人看得出来。
 *
 * 没点的项不拼进去，只在末尾点名一句「按你的推荐来」。把没点的也按推荐值写成用户的选择，
 * 等于替用户认了几个他没看的结论。
 *
 * 发出去就关掉，不留结果卡片：那句话已经进了消息区，再在旁边留一份就是同一件事摆两遍。
 */
import { Button, Drawer, Input, Radio, Space, Typography } from 'antd'
import { useState, type ReactNode } from 'react'

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

/**
 * 抽屉多高。
 *
 * 项少就只抽出装得下的那么高，项多就一直顶到离对话区顶部 30px：留这一条才看得出摆在上面的
 * 是聊天，而不是一整屏表单。
 */
function heightOf(count: number): string {
  return `min(${240 + count * 200}px, calc(100% - 30px))`
}

export default function ChoicePicker({ groups, disabled = false, onSubmit }: Props) {
  const signature = signatureOf(groups)
  const [picked, setPicked] = useState<Table>(() => defaultsOf(groups))
  const [custom, setCustom] = useState<Table>({})
  const [note, setNote] = useState<Table>({})
  const [open, setOpen] = useState(true)
  const [seen, setSeen] = useState(signature)

  // 换了一批选项就重新预选并重新抽出来：上一轮的选择与补充留着会让用户以为新的项也已经定了
  if (seen !== signature) {
    setSeen(signature)
    setPicked(defaultsOf(groups))
    setCustom({})
    setNote({})
    setOpen(true)
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
    setOpen(false)
  }

  return (
    <>
      {!open && (
        <Button block size="small" disabled={disabled} onClick={() => setOpen(true)}>
          还有 {groups.length} 项等你拍板
        </Button>
      )}
      <Drawer
        open={open}
        placement="bottom"
        height={heightOf(groups.length)}
        getContainer={false}
        rootStyle={{ position: 'absolute' }}
        title={<span style={{ fontSize: 13 }}>这几项等你拍板</span>}
        styles={{ body: { padding: 12 }, header: { padding: '8px 12px' } }}
        onClose={() => setOpen(false)}
        footer={
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
        }
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          {groups.map((one) => (
            <Space key={one.item} direction="vertical" size={6} style={{ width: '100%' }}>
              <Typography.Text strong style={{ fontSize: 13 }}>
                {one.item}
              </Typography.Text>
              <Radio.Group
                disabled={disabled}
                value={picked[one.item]}
                style={{ width: '100%' }}
                onChange={(event) =>
                  setPicked((prev) => ({ ...prev, [one.item]: event.target.value as string }))
                }
              >
                <Space direction="vertical" size={6} style={{ width: '100%' }}>
                  {one.options.map((option) => (
                    <Row key={option} active={picked[one.item] === option}>
                      <Radio value={option} style={ROW_LABEL}>
                        {option}
                        {option === one.recommended && (
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            （推荐）
                          </Typography.Text>
                        )}
                      </Radio>
                    </Row>
                  ))}
                  <Row active={picked[one.item] === CUSTOM}>
                    <Radio value={CUSTOM} style={ROW_LABEL}>
                      其他
                    </Radio>
                  </Row>
                </Space>
              </Radio.Group>
              {picked[one.item] === CUSTOM ? (
                <Input
                  size="small"
                  disabled={disabled}
                  value={custom[one.item] ?? ''}
                  placeholder="你想要的是什么样，写清楚一点"
                  onChange={(event) =>
                    setCustom((prev) => ({ ...prev, [one.item]: event.target.value }))
                  }
                />
              ) : (
                <Input
                  size="small"
                  disabled={disabled}
                  value={note[one.item] ?? ''}
                  placeholder="补充一句（可选）"
                  onChange={(event) =>
                    setNote((prev) => ({ ...prev, [one.item]: event.target.value }))
                  }
                />
              )}
            </Space>
          ))}
        </Space>
      </Drawer>
    </>
  )
}

/** 单选铺满整行：点框里任何地方都算点这一项，选项文字长了在框里自己折。 */
const ROW_LABEL = { display: 'flex', alignItems: 'flex-start', fontSize: 13 } as const

/** 一个选项一行：整行都框起来，选中的那行描边跟着变。 */
function Row({ active, children }: { active: boolean; children: ReactNode }) {
  return (
    <div
      style={{
        width: '100%',
        padding: '6px 10px',
        borderRadius: 6,
        border: `1px solid ${active ? '#1677ff' : '#f0f0f0'}`,
        background: active ? '#e6f4ff' : '#fff',
      }}
    >
      {children}
    </div>
  )
}
