/**
 * 麦克风录音：getUserMedia + MediaRecorder。
 *
 * 只管「录一段拿到 Blob」，不碰转写、不碰 UI。MediaRecorder 默认吐 webm/opus，后端 faster-whisper
 * 自带 ffmpeg 直接解，前端不转格式。权限被拒 / 没设备时 `start()` 抛错，交给上层提示。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

export interface VoiceRecorder {
  recording: boolean
  start: () => Promise<void>
  stop: () => Promise<Blob>
}

export function useVoiceRecorder(): VoiceRecorder {
  const [recording, setRecording] = useState(false)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const chunksRef = useRef<Blob[]>([])

  const release = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    recorderRef.current = null
  }, [])

  // 卸载时兜底停轨，别把麦克风一直占着。
  useEffect(() => release, [release])

  const start = useCallback(async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    const recorder = new MediaRecorder(stream)
    chunksRef.current = []
    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) chunksRef.current.push(event.data)
    }
    streamRef.current = stream
    recorderRef.current = recorder
    recorder.start()
    setRecording(true)
  }, [])

  const stop = useCallback(
    () =>
      new Promise<Blob>((resolve) => {
        const recorder = recorderRef.current
        if (!recorder) {
          resolve(new Blob())
          return
        }
        recorder.onstop = () => {
          const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' })
          release()
          setRecording(false)
          resolve(blob)
        }
        recorder.stop()
      }),
    [release],
  )

  return { recording, start, stop }
}
