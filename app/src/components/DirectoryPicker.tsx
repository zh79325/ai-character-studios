/**
 * 目录输入框 + 系统文件对话框。
 *
 * 路径也留着手敲：脱离 Electron 直接开浏览器调试时没有 preload，那时对话框按钮点不了，
 * 但粘一个绝对路径进来依然能用。
 */
import { FolderOpenOutlined } from '@ant-design/icons'
import { App, Button, Input, Space } from 'antd'

interface Props {
  value?: string
  onChange?: (value: string) => void
  placeholder?: string
  /** 对话框打开时停在哪儿，一般给默认项目根。 */
  defaultPath?: string
}

export default function DirectoryPicker({ value, onChange, placeholder, defaultPath }: Props) {
  const { message } = App.useApp()
  const bridge = window.atelier

  const browse = () => {
    if (!bridge) return
    void bridge
      .chooseDirectory(defaultPath)
      .then((picked) => {
        if (picked) onChange?.(picked)
      })
      .catch((err: unknown) => message.error(err instanceof Error ? err.message : String(err)))
  }

  return (
    <Space.Compact style={{ width: '100%' }}>
      <Input
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange?.(event.target.value)}
      />
      <Button
        icon={<FolderOpenOutlined />}
        disabled={!bridge}
        title={bridge ? '打开系统文件对话框' : '不在 Electron 里跑，手动粘路径'}
        onClick={browse}
      >
        浏览
      </Button>
    </Space.Compact>
  )
}
