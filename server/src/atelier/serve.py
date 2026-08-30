"""进程入口：Electron spawn 的就是它，单独跑起来给浏览器或别的客户端用也是它。

约定死一件事：**标准输出的第一行必须是 `ATELIER_PORT=xxxxx`**，Electron 主进程靠这行
拿到端口。端口 0 时由系统分配空闲端口，且 socket 先绑好再打印、再交给 uvicorn——
不先绑就打印会有「打印出来的端口被别人抢走」的空窗。

单独跑后端要给一个固定端口：端口每次都变的话前端没法配。

    uv run atelier-serve --port 8799             # app 那边 npm run dev:web 连的就是它
    uv run atelier-serve --port 8799 --reload    # 改后端代码自动重启
"""

from __future__ import annotations

import argparse
import socket
import sys
from collections.abc import Sequence

import uvicorn

from atelier.settings import get_settings

PORT_LINE_PREFIX = "ATELIER_PORT="

APP_PATH = "atelier.main:app"
"""给 uvicorn 的导入串而不是 app 对象：热重启要靠它在子进程里重新导入。"""


def bind_socket(host: str, port: int) -> socket.socket:
    """绑好监听 socket 并保持占用，端口 0 时由内核挑一个空闲的。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


def announce_port(port: int) -> None:
    sys.stdout.write(f"{PORT_LINE_PREFIX}{port}\n")
    sys.stdout.flush()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """命令行覆盖 settings。不给参数就是 .env 与默认值，Electron 就是这么起的。"""
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="atelier-serve", description="Atelier 本地后端")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument(
        "--port", type=int, default=settings.port, help="0 = 由系统分配空闲端口（默认）"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="改代码自动重启，单独跑后端时用；必须同时给非 0 的 --port",
    )
    args = parser.parse_args(argv)
    if args.reload and args.port == 0:
        parser.error("--reload 要一个固定端口：重启由 uvicorn 自己开子进程，绑好的 socket 传不进去")
    return args


def serve(host: str, port: int) -> None:
    """先绑 socket、再报端口、最后把这个 socket 交给 uvicorn。"""
    sock = bind_socket(host, port)
    announce_port(sock.getsockname()[1])
    config = uvicorn.Config(
        APP_PATH,
        log_level="info",
        access_log=False,
        timeout_graceful_shutdown=5,
    )
    uvicorn.Server(config).run(sockets=[sock])


def serve_reload(host: str, port: int) -> None:
    """热重启模式：端口是命令行给死的，照样先报出来，日志格式对外保持一致。"""
    announce_port(port)
    uvicorn.run(
        APP_PATH,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        reload=True,
        timeout_graceful_shutdown=5,
    )


def main() -> None:
    args = parse_args()
    if args.reload:
        serve_reload(args.host, args.port)
    else:
        serve(args.host, args.port)


if __name__ == "__main__":
    main()
