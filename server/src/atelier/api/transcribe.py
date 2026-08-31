"""转写接口：把一段录音变成文字。

无状态、不碰任何库：录音上传上来，交给本地模型转成文字就回。会话、项目一概不掺和——
它只是给输入框省一次打字。同步阻塞（`def`，FastAPI 放线程池），转写本就是「等结果」的事。
"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from atelier import voice

router = APIRouter(prefix="/api", tags=["transcribe"])


class TranscribeOut(BaseModel):
    text: str


# B008：Depends/File 这类不能直接当函数默认值写在签名里，提到模块级。
_AUDIO = File(...)


@router.post("/transcribe", response_model=TranscribeOut)
def transcribe(audio: UploadFile = _AUDIO) -> TranscribeOut:
    # 同步 `def`：转写是 CPU 密集的阻塞活，放线程池跑才不卡事件循环。同步里读底层文件句柄
    data = audio.file.read()
    try:
        text = voice.transcribe(data)
    except voice.VoiceModelMissing as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 解码失败等都归「这段音频用不了」
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"这段录音转不了：{exc}"
        ) from exc
    return TranscribeOut(text=text)
