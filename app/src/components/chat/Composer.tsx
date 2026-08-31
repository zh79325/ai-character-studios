/**
 * 输入框 + 开场短语。
 *
 * 开场短语只填不发：那句话是平台拟的，直接发出去等于拿用户的名义说了一句他没看过的话。空输入
 * 框会让人卡在「该说到多细」，给一句写好的，他改几个词就能发。
 *
 * 语音输入：点麦克风切进语音模式，长按空格录音、松开转写，识别文本追加进输入框。录音、转写期间
 * 输入框锁定并盖上波浪动画。转写走本地模型，逻辑落在这里——它就是各处复用的「对话输入模块」。
 */
import { App, Button, Input, Space, Tag, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'

import { transcribe } from '@/api/voice'
import VoiceInputButton from '@/components/chat/VoiceInputButton'
import WaveAnimation from '@/components/chat/WaveAnimation'
import { useVoiceRecorder } from '@/hooks/useVoiceRecorder'

/** 出错时尽量给后端/浏览器的原话，拿不到才退到兜底句。 */
function reason(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}

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
  const { message } = App.useApp()
  const recorder = useVoiceRecorder()
  const [voiceMode, setVoiceMode] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  // 一次「按下-松开」算一段。ref 而非 state：键盘回调里要读当下值，不能等重渲染。
  const holdingRef = useRef(false)

  useEffect(() => {
    if (!voiceMode) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.code !== 'Space' || event.repeat) return
      if (busy || transcribing || holdingRef.current) return
      event.preventDefault()
      holdingRef.current = true
      recorder.start().catch((error) => {
        holdingRef.current = false
        message.error(reason(error, '打不开麦克风，检查下权限'))
      })
    }
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.code !== 'Space' || !holdingRef.current) return
      event.preventDefault()
      holdingRef.current = false
      setTranscribing(true)
      recorder
        .stop()
        .then((blob) => transcribe(blob))
        .then((text) => {
          if (text) onChange(value ? `${value}${text}` : text)
        })
        .catch((error) => message.error(reason(error, '这段没转成文字')))
        .finally(() => setTranscribing(false))
    }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('keyup', onKeyUp)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('keyup', onKeyUp)
    }
  }, [voiceMode, busy, transcribing, recorder, onChange, value, message])

  const locked = busy || recorder.recording || transcribing

  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      {starters.map((text) => (
        <Starter key={text} text={text} onPick={() => onChange(text)} />
      ))}
      {voiceMode && (
        <Tag color={recorder.recording ? 'processing' : 'blue'}>
          {recorder.recording
            ? '正在听…松开结束'
            : transcribing
              ? '识别中…'
              : '语音模式 · 长按空格说话'}
        </Tag>
      )}
      <div style={{ position: 'relative' }}>
        <Input.TextArea
          value={value}
          rows={3}
          disabled={locked}
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
        {recorder.recording && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              background: 'rgba(255, 255, 255, 0.85)',
              border: '1px solid #1677ff',
              borderRadius: 8,
            }}
          >
            <WaveAnimation />
          </div>
        )}
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <VoiceInputButton
          active={voiceMode}
          disabled={busy || transcribing}
          onToggle={() => setVoiceMode((on) => !on)}
        />
        <Button
          type="primary"
          style={{ flex: 1 }}
          loading={busy}
          disabled={busy}
          onClick={onSubmit}
        >
          {busy ? '等回话' : '发送'}
        </Button>
      </div>
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
