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
 * 单选还是多选由 Agent 逐项声明（`multiple`）：互排的维度只能拍一个，可叠加的维度（参考作品、
 * 要避开的元素）本来就是好几项。
 *
 * 单选题留「其他」：列出来的几个覆盖不到用户真正想要的时候，逼他在这几个里挑一个等于替他改了
 * 需求。选了「其他」就只写自填值，不再多给一个补充框——同一件事两个框，用户得猜该写哪个。
 * 多选题不给「其他」：想加的直接写进那一个输入框，多勾一个「其他」再写一遍是白绕一道。
 *
 * 点完拼出来的那句话用的是 Agent 自己给的字面值，不写「选 B」：指代要靠模型回头数选项，
 * 而它数错的时候没人看得出来。
 *
 * 没点的项不拼进去，只在末尾点名一句「按你的推荐来」。把没点的也按推荐值写成用户的选择，
 * 等于替用户认了几个他没看的结论。
 *
 * 发出去就关掉，不留结果卡片：那句话已经进了消息区，再在旁边留一份就是同一件事摆两遍。
 *
 * `finale` 摆在所有题后面（立项页传的是确认游戏风格、确认立项）：收口本身也是用户得拍的一项，
 * 跟其他题摆在一处才看得出「拍完这几项就能收口，也可以接着聊」。它拿到一个关抽屉的手，
 * 收口顺手要发一句话给 Agent 时先把这层收掉——不然设计师在后面回话，用户对着一层遮罩等。
 */
import { Button, Checkbox, Divider, Drawer, Input, Radio, Space, Typography } from 'antd'
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
  /** 摆在最后一题后面的收口动作（立项页：确认游戏风格、确认立项）。 */
  finale?: ((close: () => void) => ReactNode) | null
  /** 收口这一步叫什么：没有待选项时它就是抽屉标题与那个重开按钮上的字。 */
  finaleTitle?: string
}

type Table = Record<string, string>
/** 多选项当下勾了哪几个。 */
type Marks = Record<string, string[]>

/** 一批选项的内容签名：详情每次刷新都是新数组，认内容才知道是不是换了一批。 */
function signatureOf(groups: ChoiceGroup[]): string {
  return groups
    .map((one) => `${one.item}=${one.multiple ? '*' : ''}${one.options.join('|')}`)
    .join('\n')
}

function singleDefaults(groups: ChoiceGroup[]): Table {
  const picked: Table = {}
  for (const one of groups) {
    const first = one.recommended[0]
    if (!one.multiple && first !== undefined) picked[one.item] = first
  }
  return picked
}

function multiDefaults(groups: ChoiceGroup[]): Marks {
  const marks: Marks = {}
  for (const one of groups) {
    if (one.multiple) marks[one.item] = [...one.recommended]
  }
  return marks
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

export default function ChoicePicker({
  groups,
  disabled = false,
  onSubmit,
  finale = null,
  finaleTitle = '收口',
}: Props) {
  const signature = signatureOf(groups)
  const [picked, setPicked] = useState<Table>(() => singleDefaults(groups))
  const [marks, setMarks] = useState<Marks>(() => multiDefaults(groups))
  const [custom, setCustom] = useState<Table>({})
  const [note, setNote] = useState<Table>({})
  // 有题才自己抽出来：只剩收口那一块时抽出来会白盖住对话，用户还没想收口
  const [open, setOpen] = useState(groups.length > 0)
  const [seen, setSeen] = useState(signature)

  // 换了一批选项就重新预选并重新抽出来：上一轮的选择与补充留着会让用户以为新的项也已经定了
  if (seen !== signature) {
    setSeen(signature)
    setPicked(singleDefaults(groups))
    setMarks(multiDefaults(groups))
    setCustom({})
    setNote({})
    if (groups.length > 0) setOpen(true)
  }

  if (groups.length === 0 && finale === null) return null

  /** 这一项当下定成了什么。选了「其他」却没写字、多选一个没勾都算还没定。 */
  const valueOf = (one: ChoiceGroup): string => {
    if (one.multiple) return (marks[one.item] ?? []).join('、')
    const chosen = picked[one.item]
    if (chosen === undefined) return ''
    return chosen === CUSTOM ? (custom[one.item] ?? '').trim() : chosen
  }

  const noteOf = (item: string): string => (note[item] ?? '').trim()

  // 多选题那个输入框就是它的自填口：一个没勾但写了字，这一项也算定了
  const settled = groups.filter(
    (one) => valueOf(one) !== '' || (one.multiple && noteOf(one.item) !== ''),
  )

  const compose = (): string => {
    const lines = settled.map((one) => {
      const value = valueOf(one)
      const extra = noteOf(one.item)
      if (value === '') return `- ${one.item}: ${extra}`
      return `- ${one.item}: ${value}${extra === '' ? '' : `（补充：${extra}）`}`
    })
    const rest = groups.filter((one) => !settled.includes(one)).map((one) => one.item)
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
          {groups.length > 0 ? `还有 ${groups.length} 项等你拍板` : finaleTitle}
        </Button>
      )}
      <Drawer
        open={open}
        placement="bottom"
        height={heightOf(groups.length + (finale === null ? 0 : 1))}
        getContainer={false}
        rootStyle={{ position: 'absolute' }}
        title={
          <span style={{ fontSize: 13 }}>{groups.length > 0 ? '这几项等你拍板' : finaleTitle}</span>
        }
        styles={{ body: { padding: 12 }, header: { padding: '8px 12px' } }}
        onClose={() => setOpen(false)}
        footer={
          groups.length === 0 ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              还想接着聊就关掉这层，回到下面的输入框
            </Typography.Text>
          ) : (
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
          )
        }
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          {groups.map((one) => (
            <Space key={one.item} direction="vertical" size={6} style={{ width: '100%' }}>
              <Space size={6}>
                <Typography.Text strong style={{ fontSize: 13 }}>
                  {one.item}
                </Typography.Text>
                {one.multiple && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    可多选
                  </Typography.Text>
                )}
              </Space>
              {one.multiple ? (
                <Checkbox.Group
                  disabled={disabled}
                  value={marks[one.item] ?? []}
                  style={{ width: '100%' }}
                  onChange={(next) =>
                    setMarks((prev) => ({ ...prev, [one.item]: next as string[] }))
                  }
                >
                  <Space direction="vertical" size={6} style={{ width: '100%' }}>
                    {one.options.map((option) => (
                      <Row key={option} active={(marks[one.item] ?? []).includes(option)}>
                        <Checkbox value={option} style={ROW_LABEL}>
                          {option}
                          {one.recommended.includes(option) && <Recommended />}
                        </Checkbox>
                      </Row>
                    ))}
                  </Space>
                </Checkbox.Group>
              ) : (
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
                          {one.recommended.includes(option) && <Recommended />}
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
              )}
              {!one.multiple && picked[one.item] === CUSTOM ? (
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
                  placeholder={one.multiple ? '还想要别的就写这儿（可选）' : '补充一句（可选）'}
                  onChange={(event) =>
                    setNote((prev) => ({ ...prev, [one.item]: event.target.value }))
                  }
                />
              )}
            </Space>
          ))}
          {finale !== null && (
            <>
              {groups.length > 0 && <Divider style={{ margin: 0 }} />}
              {finale(() => setOpen(false))}
            </>
          )}
        </Space>
      </Drawer>
    </>
  )
}

/** 单选铺满整行：点框里任何地方都算点这一项，选项文字长了在框里自己折。 */
export const ROW_LABEL = { display: 'flex', alignItems: 'flex-start', fontSize: 13 } as const

function Recommended() {
  return (
    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
      （推荐）
    </Typography.Text>
  )
}

/** 一个选项一行：整行都框起来，选中的那行描边跟着变。`finale` 里的选项跟它共用一套长相。 */
export function Row({ active, children }: { active: boolean; children: ReactNode }) {
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
