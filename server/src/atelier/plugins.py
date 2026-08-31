"""插件安装：后台把「用得上但不随代码走」的大件下载到本地。

每个插件自带两件套：`is_installed`（检测装没装好）、`install`（阻塞式安装，边下边回调进度）。
运行器（本模块下半段）只管：起后台线程、记状态、按回调上报的字节数算百分比与剩余时间，
前端轮询 REST 拿状态。状态只存进程内存，不落库；进程重启没装完就重来，按文件大小断点续传。

第一个（目前唯一）插件是语音识别模型（faster-whisper large-v3，约 3GB），从国内镜像
hf-mirror 直连下载：逐文件 HTTP GET + Range 续传，直接写进模型目录，不碰 huggingface_hub
的本地缓存（省掉 xet 绕美区 CDN、`.incomplete` 残留一堆的老毛病）。
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import httpx
import structlog

from atelier import voice

_log = structlog.get_logger(__name__)

# 进度回调：把「已下字节, 总字节」告诉运行器。total 为 0 表示总量还没算出来。
ProgressCb = Callable[[int, int], None]


@dataclass(frozen=True)
class Plugin:
    """一个可安装插件：一段静态描述 + 两个行为函数。"""

    id: str
    name: str
    description: str
    is_installed: Callable[[], bool]
    """检测装没装好。"""
    install: Callable[[ProgressCb], None]
    """阻塞式安装：边下边调 on_progress(done, total)，失败抛异常。"""


# ---- HuggingFace 直连下载（第一个插件复用） ------------------------------------

# 走国内镜像。容许用环境变量覆盖（跟 huggingface_hub 一个约定）。
_HF_ENDPOINT = os.environ.get("HF_ENDPOINT") or "https://hf-mirror.com"
_CHUNK = 1024 * 1024  # 1MB，也是上报进度的粒度
_MAX_RETRIES = 7
_TIMEOUT = httpx.Timeout(30.0, connect=20.0)


def _ignored(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(name, pat) for pat in patterns)


def _download_file(*, url: str, dest: Path, expected: int, on_bytes: Callable[[int], None]) -> None:
    """单文件下载：先写到 `{dest}.part`，下满再原子改名成 dest。Range 续传 + 指数退避重试。

    只在完整时才出现正式文件名，避免半截 model.bin 被 is_installed 误判成已装。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 已经有完整的正式文件（上次装过 / 断点续传里这个文件先下完了）就跳过。
    if dest.exists():
        cur = dest.stat().st_size
        if not expected or cur == expected:
            on_bytes(expected or cur)
            return
        dest.unlink()  # 大小对不上，删了重下

    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, _MAX_RETRIES + 1):
        have = part.stat().st_size if part.exists() else 0
        if expected and have > expected:  # 脏残片，重下
            have = 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with httpx.stream(
                "GET", url, headers=headers, follow_redirects=True, timeout=_TIMEOUT
            ) as resp:
                if have and resp.status_code == 416:  # 服务端说没得续了 = 已下满
                    if part.exists():
                        os.replace(part, dest)
                    on_bytes(expected or have)
                    return
                resp.raise_for_status()
                # 服务端可能无视 Range 从头给（返 200），那就重头写，别往残片后面追。
                resumed = have > 0 and resp.status_code == 206
                downloaded = have if resumed else 0
                with part.open("ab" if resumed else "wb") as f:
                    for chunk in resp.iter_bytes(_CHUNK):
                        f.write(chunk)
                        downloaded += len(chunk)
                        on_bytes(downloaded)
            os.replace(part, dest)
            return
        except (httpx.HTTPError, OSError) as exc:
            _log.warning("plugin_download_retry", file=dest.name, attempt=attempt, error=str(exc))
            if attempt >= _MAX_RETRIES:
                raise
            time.sleep(min(2**attempt, 30))


def _download_hf_model(
    *,
    repo_id: str,
    target_dir: Path,
    on_progress: ProgressCb,
    ignore: tuple[str, ...] = (),
) -> None:
    """把一个 HF 仓库的文件逐个拉到 target_dir（扁平）。测试里整个替换掉，不触网。"""
    from huggingface_hub import HfApi

    info = HfApi(endpoint=_HF_ENDPOINT).model_info(repo_id, files_metadata=True)
    files = [
        (sibling.rfilename, int(sibling.size or 0))
        for sibling in info.siblings
        if not _ignored(sibling.rfilename, ignore)
    ]
    total = sum(size for _, size in files)
    _log.info(
        "plugin_download_begin",
        repo=repo_id,
        files=len(files),
        total_mb=round(total / 1_000_000, 1),
        endpoint=_HF_ENDPOINT,
    )
    done = 0
    for name, size in files:
        _download_file(
            url=f"{_HF_ENDPOINT}/{repo_id}/resolve/main/{name}",
            dest=target_dir / name,
            expected=size,
            on_bytes=lambda cur, base=done: on_progress(base + cur, total),
        )
        done += size
        on_progress(done, total)


# ---- 具体插件 ------------------------------------------------------------------


def _whisper_is_installed() -> bool:
    """有 model.bin 才算装好——空目录 / 只下了一半都不算。"""
    return (voice.MODEL_DIR / "model.bin").is_file()


def _whisper_install(on_progress: ProgressCb) -> None:
    _download_hf_model(
        repo_id="Systran/faster-whisper-large-v3",
        target_dir=voice.MODEL_DIR,
        on_progress=on_progress,
        ignore=(".gitattributes", "README.md", ".git*"),
    )
    if not _whisper_is_installed():
        raise RuntimeError("下载完却没找到 model.bin")


_VOICE_PLUGIN = Plugin(
    id="whisper-large-v3",
    name="语音识别模型",
    description="本地语音转写模型（faster-whisper large-v3），装上后对话输入框才能用语音。",
    is_installed=_whisper_is_installed,
    install=_whisper_install,
)

PLUGINS: list[Plugin] = [_VOICE_PLUGIN]
_BY_ID = {plugin.id: plugin for plugin in PLUGINS}


# ---- 运行器：后台线程 + 状态 + 进度/剩余时间 -----------------------------------


@dataclass
class _Progress:
    """一个插件当前的安装态。仅在安装中/失败/刚完成时有意义。"""

    status: str = "idle"  # idle | running | done | error
    message: str = ""
    downloaded: int = 0
    total: int = 0
    samples: deque[tuple[float, int]] = field(default_factory=deque, repr=False)
    """最近若干个 (时刻, 已下字节) 采样点，只留最近一个窗口，用来算实时速率。"""
    thread: threading.Thread | None = field(default=None, repr=False)


_progress: dict[str, _Progress] = {}
_lock = threading.Lock()

# 速率只看最近这段时间：整段会话平均会把开头列文件/建连的死时间算进去，
# 导致剩余时间虚高还越算越长。用滑动窗口才灵敏、剩余时间会真的往下走。
_RATE_WINDOW = 8.0  # 秒


def _rate_bytes(prog: _Progress | None) -> float | None:
    """最近一个窗口内的下载速率（字节/秒）。采样不足或没新增就返回 None。"""
    if prog is None or prog.status != "running" or len(prog.samples) < 2:
        return None
    t0, b0 = prog.samples[0]
    t1, b1 = prog.samples[-1]
    span = t1 - t0
    delta = b1 - b0
    if span <= 0 or delta <= 0:
        return None
    return delta / span


def _eta_seconds(prog: _Progress | None) -> int | None:
    """预估剩余秒数：剩余字节 / 当前速率。测不到速率或不知总量就返回 None。"""
    rate = _rate_bytes(prog)
    if rate is None or prog is None or prog.total <= 0:
        return None
    remaining = max(0, prog.total - prog.downloaded)
    return int(remaining / rate)


def _status_dict(plugin: Plugin) -> dict:
    with _lock:
        prog = _progress.get(plugin.id)
    running = bool(prog and prog.status == "running")
    done_ok = bool(prog and prog.status == "done")
    # 装没装好以安装任务实际结束为准：还在跑就不算装好，哪怕 model.bin 已落地。
    installed = (done_ok or plugin.is_installed()) and not running
    message = prog.message if prog and prog.status == "error" else ""
    if running and prog and prog.total > 0:
        progress = max(0, min(99, round(prog.downloaded / prog.total * 100)))
    elif running:
        progress = 0
    else:
        progress = 100 if installed else 0
    rate = _rate_bytes(prog)
    return {
        "id": plugin.id,
        "name": plugin.name,
        "description": plugin.description,
        "installed": installed,
        "running": running,
        "progress": progress,
        "eta_seconds": _eta_seconds(prog),
        "downloaded_bytes": prog.downloaded if prog else 0,
        "total_bytes": prog.total if prog else 0,
        "speed_bytes": int(rate) if rate else None,
        "message": message,
    }


def _install_worker(plugin: Plugin) -> None:
    _log.info("plugin_install_start", plugin=plugin.id)

    def on_progress(done: int, total: int) -> None:
        with _lock:
            prog = _progress.get(plugin.id)
            if prog is None:
                return
            prog.downloaded = done
            prog.total = total
            now = time.monotonic()
            prog.samples.append((now, done))
            cutoff = now - _RATE_WINDOW  # 掐掉窗口外的老采样，但至少留两个好算速率
            while len(prog.samples) > 2 and prog.samples[0][0] < cutoff:
                prog.samples.popleft()

    try:
        plugin.install(on_progress)
        if not plugin.is_installed():
            raise RuntimeError("安装跑完却没检测到已装")
        with _lock:
            cur = _progress.get(plugin.id)
            _progress[plugin.id] = _Progress(
                status="done",
                message="安装完成",
                downloaded=cur.downloaded if cur else 0,
                total=cur.total if cur else 0,
            )
        _log.info("plugin_install_done", plugin=plugin.id)
    except Exception as exc:  # noqa: BLE001 — 网络/磁盘什么都可能炸，统一记进状态给前端看
        with _lock:
            _progress[plugin.id] = _Progress(status="error", message=str(exc))
        _log.exception("plugin_install_failed", plugin=plugin.id)


def list_plugins() -> list[dict]:
    return [_status_dict(plugin) for plugin in PLUGINS]


def plugin_status(plugin_id: str) -> dict:
    """查一个插件的状态。未知 id 抛 KeyError（路由翻 404）。"""
    return _status_dict(_BY_ID[plugin_id])


def start_install(plugin_id: str) -> dict:
    """触发安装并立刻返回当前状态。已装好或正在装都不重复起线程。"""
    plugin = _BY_ID[plugin_id]
    if not plugin.is_installed():
        with _lock:
            prog = _progress.get(plugin_id)
            if not (prog and prog.status == "running"):
                thread = threading.Thread(target=_install_worker, args=(plugin,), daemon=True)
                _progress[plugin_id] = _Progress(status="running", thread=thread)
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
