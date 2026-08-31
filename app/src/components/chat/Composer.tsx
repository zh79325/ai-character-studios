/**
 * 输入框 + 开场短语。
 *
 * 开场短语只填不发：那句话是平台拟的，直接发出去等于拿用户的名义说了一句他没看过的话。空输入
 * 框会让人卡在「该说到多细」，给一句写好的，他改几个词就能发。
 */
import { Button, Input, Space, Typography } from 'antd'

export default function Composer({
  value,
  onChange,
  onSubmit,
  busy,
  who,
  starters,
}: {
  value: string
  onChange: (text: string) => void
  onSubmit: () => void
  busy: boolean
  /** 对面那位怎么称呼，只用在等待与占位文案里。 */
  who: string
  /** 摆在输入框上面的示例说辞。什么时候该摆由调用方判断，不摆就给空数组。 */
  starters: string[]
}) {
  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      {starters.map((text) => (
        <Starter key={text} text={text} onPick={() => onChange(text)} />
      ))}
      <Input.TextArea
        value={value}
        rows={3}
        disabled={busy}
        placeholder={
          busy ? `等${who}回这一轮，回完再接着说` : '说清你要什么。Enter 发送，Shift+Enter 换行'
        }
        onChange={(event) => onChange(event.target.value)}
        onPressEnter={(event) => {
          if (event.shiftKey) return
          event.preventDefault()
          onSubmit()
        }}
      />
      <Button type="primary" block loading={busy} disabled={busy} onClick={onSubmit}>
        {busy ? '等回话' : '发送'}
      </Button>
    </Space>
  )
}

function Starter({ text, onPick }: { text: string; onPick: () => void }) {
  return (
    <div
      onClick={onPick}
      style={{
        border: '1px solid #f0f0f0',
        borderRadius: 6,
        padding: '8px 10px',
        background: '#fafafa',
        cursor: 'pointer',
      }}
    >
      <Space direction="vertical" size={2}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          不知道从哪儿说起，点这句填进输入框再改：
        </Typography.Text>
        <Typography.Text style={{ fontSize: 12 }}>{text}</Typography.Text>
      </Space>
    </div>
  )
}
