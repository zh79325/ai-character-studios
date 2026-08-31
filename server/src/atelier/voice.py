"""语音转写：本地 Faster-Whisper。

模型不随进程启动加载——那是个 1.5GB 起步、加载要好几秒的家伙，绝大多数会话根本不说话。
第一次真的要转写时才把它读进内存，之后常驻。加载与转写分成两个函数，测试里各自替换掉，
不必碰真实模型。

模型文件不进代码仓库：固定放在仓库 `models/whisper-large-v3/`（不可配置）。本地没有就自动从
gitee 模型仓库 `git clone` 进去；克隆不下来（没网 / 没配 SSH）才抛 `VoiceModelMissing`，
路由把它翻成一句能照着做的提示。
"""

from __future__ import annotations

import subprocess
import threading
from io import BytesIO
from typing import TYPE_CHECKING

from atelier.settings import REPO_ROOT, get_settings

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

# 固定路径 + 固定下载地址，都不走配置。
MODEL_DIR = REPO_ROOT / "models" / "whisper-large-v3"
MODEL_GIT_URL = "git@gitee.com:tudou888888/local-speech-recognition-model.git"

_model: WhisperModel | None = None
_lock = threading.Lock()


class VoiceModelMissing(RuntimeError):
    """转写模型不在本地、又没能自动克隆下来。→ 503"""


def _has_model() -> bool:
    """有 model.bin 才算装好——空目录 / 只克隆了一半都不算。"""
    return (MODEL_DIR / "model.bin").is_file()


def _ensure_model() -> None:
    """本地没有模型就从 gitee 克隆进固定目录。克隆失败翻成 VoiceModelMissing。"""
    if _has_model():
        return
    MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
    # 目录残留（空目录 / 上次没克隆完）会让 git clone 报错，先清掉。
    if MODEL_DIR.exists():
        import shutil

        shutil.rmtree(MODEL_DIR)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", MODEL_GIT_URL, str(MODEL_DIR)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise VoiceModelMissing(
            f"语音模型没装上：本地没有 {MODEL_DIR}，自动克隆也失败了"
            f"（{MODEL_GIT_URL}）。手动执行 git clone 把它放到该目录即可。\n{detail}"
        ) from exc
    if not _has_model():
        raise VoiceModelMissing(f"克隆完却没找到 model.bin：检查 {MODEL_DIR} 里的内容。")


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
