"""进程入口：Electron spawn 的就是它，单独跑起来给浏览器或别的客户端用也是它。

端口是固定的（默认 8799，`ATELIER_PORT` 或 `--port` 可改）。固定端口才谈得上「后端已经在跑就
直接用」：Electron 启动先对这个端口探一下 `/api/health`，活的就不再 spawn——两个后端同时开着会抢
同一份 SQLite，还会把会话发到不是你看日志那个进程上。

标准输出的第一行仍是 `ATELIER_PORT=xxxxx`，Electron spawn 后靠它确认端口已就绪。

    uv run atelier-serve                         # 默认 8799
    uv run atelier-serve --port 8799 --reload    # 改后端代码自动重启
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import uvicorn

from atelier.settings import get_settings

PORT_LINE_PREFIX = "ATELIER_PORT="

APP_PATH = "atelier.main:app"
"""给 uvicorn 的导入串而不是 app 对象：热重启要靠它在子进程里重新导入。"""


def announce_port(port: int) -> None:
    sys.stdout.write(f"{PORT_LINE_PREFIX}{port}\n")
    sys.stdout.flush()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """命令行覆盖 settings。不给参数就是 .env 与默认值，Electron 就是这么起的。"""
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="atelier-serve", description="Atelier 本地后端")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port, help="固定端口，默认 8799")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="改代码自动重启，单独跑后端时用",
    )
    args = parser.parse_args(argv)
    if not 0 < args.port <= 65535:
        parser.error("--port 要一个 1-65535 的固定端口")
    return args


def serve(host: str, port: int, *, reload: bool = False) -> None:
    """先报端口再交给 uvicorn。端口被别人占着就让 uvicorn 直接报 address in use。"""
    announce_port(port)
    uvicorn.run(
        APP_PATH,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        reload=reload,
        timeout_graceful_shutdown=5,
    )


def main() -> None:
    args = parse_args()
    serve(args.host, args.port, reload=args.reload)


if __name__ == "__main__":
    main()
