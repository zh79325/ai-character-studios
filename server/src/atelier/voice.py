"""语音转写：本地 Faster-Whisper。

模型不随进程启动加载——那是个 1.5GB 起步、加载要好几秒的家伙，绝大多数会话根本不说话。
第一次真的要转写时才把它读进内存，之后常驻。加载与转写分成两个函数，测试里各自替换掉，
不必碰真实模型。

模型文件不进代码仓库：固定放在仓库 `models/whisper-large-v3/`（不可配置）。它由「插件管理」在后台
下载安装（见 `plugins.py`），这里只负责加载与转写。目录里没有 `model.bin` 就当「还没装」，抛
`VoiceModelMissing`，路由把它翻成一句「去插件管理装一下」的提示。
"""

from __future__ import annotations

import threading
from io import BytesIO
from typing import TYPE_CHECKING

from atelier.settings import REPO_ROOT, get_settings

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

# 模型固定落在这里，不走配置。下载安装由 plugins.py 负责。
MODEL_DIR = REPO_ROOT / "models" / "whisper-large-v3"

_model: WhisperModel | None = None
_lock = threading.Lock()


class VoiceModelMissing(RuntimeError):
    """转写模型还没装。→ 503"""


def _has_model() -> bool:
    """有 model.bin 才算装好——空目录 / 只下了一半都不算。"""
    return (MODEL_DIR / "model.bin").is_file()


def _ensure_model() -> None:
    """模型不在就直接报缺失，引到插件管理去装——下载 3GB 不能阻在一次转写请求里。"""
    if not _has_model():
        raise VoiceModelMissing(
            "语音模型还没装。去「系统状态 → 插件管理」安装「语音识别模型」后再试。"
        )


def load_model() -> WhisperModel:
    """拿到（必要时先加载）转写模型。加锁：并发首个请求只加载一次。"""
    global _model
    with _lock:
        if _model is not None:
            return _model
        _ensure_model()
        from faster_whisper import WhisperModel

        settings = get_settings()
        _model = WhisperModel(
            str(MODEL_DIR),
            device=settings.asr_device,
            compute_type=settings.asr_compute_type,
        )
        return _model


def transcribe(audio: bytes) -> str:
    """把一段音频转成文字。webm/opus 直接交给它——PyAV 自带的 ffmpeg 会解码。"""
    if not audio:
        raise ValueError("音频是空的")
    model = load_model()
    settings = get_settings()
    segments, _ = model.transcribe(
        BytesIO(audio),
        language=settings.asr_language,
        vad_filter=True,
    )
    return "".join(segment.text for segment in segments).strip()


def _reset_for_tests() -> None:
    """测试隔离用：卸掉已加载的模型。"""
    global _model
    with _lock:
        _model = None
