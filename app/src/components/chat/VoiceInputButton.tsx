/**
 * 语音输入开关：点一下切进/切出语音模式。图标随状态变实心。
 */
import { AudioOutlined, AudioMutedOutlined } from '@ant-design/icons'
import { Button, Tooltip } from 'antd'

export default function VoiceInputButton({
  active,
  disabled,
  onToggle,
}: {
  active: boolean
  disabled?: boolean
  onToggle: () => void
}) {
  return (
    <Tooltip title={active ? '关闭语音输入' : '开启语音输入'}>
      <Button
        type={active ? 'primary' : 'default'}
        disabled={disabled}
        icon={active ? <AudioOutlined /> : <AudioMutedOutlined />}
        onClick={onToggle}
      />
    </Tooltip>
  )
}
