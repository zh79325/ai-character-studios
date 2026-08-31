"""插件安装：后台把「用得上但不随代码走」的大件下载到本地。

第一个（目前也是唯一）插件是语音识别模型（faster-whisper large-v3，约 3GB）。安装 = 从国内镜像
（hf-mirror）把模型文件下载到它的固定目录。下载耗时长，放后台线程跑，接口立刻返回，前端轮询状态。

状态只存进程内存（模块级单例 + 锁），不落库：安装是一次性动作，进程重启后没装完就重来即可，
huggingface_hub 的本地缓存还能断点续传。进度没有精确回调，用「目标目录已下字节 / 预期总大小」估。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

from atelier import voice


@dataclass(frozen=True)
class Plugin:
    """一个可安装插件的静态描述。"""

    id: str
    name: str
    description: str
    repo_id: str
    """HuggingFace 上的仓库 id，snapshot_download 照它拉。"""
    target_dir_name: str
    """落地目录名，父目录固定是仓库 models/。"""
    expected_bytes: int
    """预期总大小，只用来估进度百分比，不必精确。"""
    marker: str = "model.bin"
    """有这个文件才算装好。"""


# faster-whisper large-v3：转写用的本地模型。目标目录跟 voice.MODEL_DIR 是同一个。
_VOICE_PLUGIN = Plugin(
    id="whisper-large-v3",
    name="语音识别模型",
    description="本地语音转写模型（faster-whisper large-v3），装上后对话输入框才能用语音。",
    repo_id="Systran/faster-whisper-large-v3",
    target_dir_name="whisper-large-v3",
    expected_bytes=3_100_000_000,
)

PLUGINS: list[Plugin] = [_VOICE_PLUGIN]
_BY_ID = {plugin.id: plugin for plugin in PLUGINS}


@dataclass
class _Progress:
    """一个插件当前的安装态。仅在安装中/失败时有意义。"""

    status: str = "idle"  # idle | running | done | error
    message: str = ""
    started_at: float | None = None
    """开装时间戳（time.monotonic），算下载速率与剩余时间用。"""
    start_bytes: int = 0
    """开装时目录里已有的字节（断点续传的基线）。速率只算本次新增的，不把旧数据算进去。"""
    thread: threading.Thread | None = field(default=None, repr=False)


_progress: dict[str, _Progress] = {}
_lock = threading.Lock()

# 下载走国内镜像、禁用 xet（xet 后端在镜像上会 401）。用 setdefault，容许用户用环境变量覆盖。
_HF_ENDPOINT = "https://hf-mirror.com"


def _target_dir(plugin: Plugin):
    return voice.MODEL_DIR.parent / plugin.target_dir_name


def _is_installed(plugin: Plugin) -> bool:
    return (_target_dir(plugin) / plugin.marker).is_file()


def _disk_bytes(plugin: Plugin) -> int:
    root = _target_dir(plugin)
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _percent(plugin: Plugin, running: bool) -> int:
    if _is_installed(plugin):
        return 100
    if not running:
        return 0
    done = _disk_bytes(plugin)
    return max(0, min(99, round(done / plugin.expected_bytes * 100)))


def _eta_seconds(plugin: Plugin, running: bool, prog: _Progress | None) -> int | None:
    """预估剩余秒数。速率只算「本次新增字节 / 已耗时」，再除剩余字节。

    断点续传时目录里已有上次的字节，所以要减掉开装时的基线 `start_bytes`，
    否则 done 一上来就很大而 elapsed 接近 0，速率被严重高估。还没新增字节时返回
    None（前端显「估算中」）。
    """
    if not running or prog is None or prog.started_at is None:
        return None
    elapsed = time.monotonic() - prog.started_at
    done = _disk_bytes(plugin)
    downloaded = done - prog.start_bytes  # 本次会话新下的
    if elapsed <= 0 or downloaded <= 0:
        return None
    rate = downloaded / elapsed  # 字节/秒
    remaining = max(0, plugin.expected_bytes - done)
    return int(remaining / rate)


def _status_dict(plugin: Plugin) -> dict:
    with _lock:
        prog = _progress.get(plugin.id)
    running = bool(prog and prog.status == "running")
    installed = _is_installed(plugin)
    message = ""
    if prog and prog.status == "error":
        message = prog.message
    return {
        "id": plugin.id,
        "name": plugin.name,
        "description": plugin.description,
        "installed": installed,
        "running": running,
        "progress": _percent(plugin, running),
        "eta_seconds": _eta_seconds(plugin, running, prog),
        "message": message,
    }


def _download(plugin: Plugin) -> None:
    """真正拉模型。测试里整个替换掉，不触网。"""
    # HF_ENDPOINT / HF_HUB_DISABLE_XET 是在 huggingface_hub 导入时读进 constants 的，
    # 改环境变量为时已晚。所以这里既显式传 endpoint，又在运行时直接翻
    # constants.HF_HUB_DISABLE_XET —— is_xet_available() 是每次调用动态读它的。
    # 不禁 xet 的话，即便 endpoint 指到镜像，xet 也会绕去美区 CDN 拖死下载。
    os.environ.setdefault("HF_ENDPOINT", _HF_ENDPOINT)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from huggingface_hub import constants, snapshot_download

    constants.HF_HUB_DISABLE_XET = True

    snapshot_download(
        repo_id=plugin.repo_id,
        local_dir=str(_target_dir(plugin)),
        endpoint=_HF_ENDPOINT,
        ignore_patterns=[".gitattributes", "README.md", ".git*"],
    )


def _install_worker(plugin: Plugin) -> None:
    try:
        _download(plugin)
        if not _is_installed(plugin):
            raise RuntimeError(f"下载完却没找到 {plugin.marker}")
        with _lock:
            _progress[plugin.id] = _Progress(status="done", message="安装完成")
    except Exception as exc:  # noqa: BLE001 — 网络/磁盘什么都可能炸，统一记进状态给前端看
        with _lock:
            _progress[plugin.id] = _Progress(status="error", message=str(exc))


def list_plugins() -> list[dict]:
    return [_status_dict(plugin) for plugin in PLUGINS]


def plugin_status(plugin_id: str) -> dict:
    """查一个插件的状态。未知 id 抛 KeyError（路由翻 404）。"""
    return _status_dict(_BY_ID[plugin_id])


def start_install(plugin_id: str) -> dict:
    """触发安装并立刻返回当前状态。已装好或正在装都不重复起线程。"""
    plugin = _BY_ID[plugin_id]
    if not _is_installed(plugin):
        with _lock:
            prog = _progress.get(plugin_id)
            if not (prog and prog.status == "running"):
                thread = threading.Thread(target=_install_worker, args=(plugin,), daemon=True)
                _progress[plugin_id] = _Progress(
                    status="running",
                    started_at=time.monotonic(),
                    start_bytes=_disk_bytes(plugin),
                    thread=thread,
                )
                thread.start()
    return _status_dict(plugin)


def _join_for_tests(plugin_id: str, timeout: float = 5.0) -> None:
    """测试用：等安装线程跑完，好断言终态。"""
    with _lock:
        prog = _progress.get(plugin_id)
    if prog and prog.thread:
        prog.thread.join(timeout)


def _reset_for_tests() -> None:
    with _lock:
        _progress.clear()
