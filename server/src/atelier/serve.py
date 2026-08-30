"""进程入口：Electron spawn 的就是它。

约定死一件事：**标准输出的第一行必须是 `ATELIER_PORT=xxxxx`**，Electron 主进程靠这行
拿到端口。端口 0 时由系统分配空闲端口，且 socket 先绑好再打印、再交给 uvicorn——
不先绑就打印会有「打印出来的端口被别人抢走」的空窗。
"""

from __future__ import annotations

import socket
import sys

import uvicorn

from atelier.settings import get_settings

PORT_LINE_PREFIX = "ATELIER_PORT="


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


def main() -> None:
    settings = get_settings()
    sock = bind_socket(settings.host, settings.port)
    announce_port(sock.getsockname()[1])

    config = uvicorn.Config(
        "atelier.main:app",
        log_level="info",
        access_log=False,
        timeout_graceful_shutdown=5,
    )
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
