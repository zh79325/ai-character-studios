/**
 * 转写接口：把一段录音发给后端，换回文字。
 *
 * 走原生 `fetch` 而不是 `client.request`——那条通道只发 JSON，这里要发 `FormData`（multipart）。
 * 基址、错误解析仍复用 `client` 里那套，保持和别处一致。
 */
import { baseUrl, ApiError, errorMessage } from './client'

interface TranscribeOut {
  text: string
}

/** 上传录音，拿回识别文本。字段名 `audio`，跟后端 `UploadFile` 对齐。 */
export async function transcribe(blob: Blob): Promise<string> {
  const base = await baseUrl()
  const form = new FormData()
  form.append('audio', blob, 'clip.webm')
  const response = await fetch(`${base}/api/transcribe`, { method: 'POST', body: form })
  if (!response.ok) throw new ApiError(response.status, await errorMessage(response))
  const body = (await response.json()) as TranscribeOut
  return body.text
}
