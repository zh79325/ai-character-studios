"""双库迁移入口：一条命令把两库都升到 head。

uv run atelier-migrate            # 两库 upgrade head
uv run atelier-migrate --db config
uv run atelier-migrate --revision -1 --db runtime
"""

from __future__ import annotations

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config

SERVER_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = SERVER_ROOT / "alembic.ini"
DB_NAMES = ("config", "runtime")


def _alembic_config(name: str) -> Config:
    cfg = Config(str(ALEMBIC_INI), ini_section=name)
    # script_location 在 ini 里是相对 server/ 的路径，这里补成绝对路径，
    # 使得从任意 cwd 调用都能定位 version 目录。
    cfg.set_main_option("script_location", str(SERVER_ROOT / f"alembic/{name}"))
    return cfg


def upgrade(name: str, revision: str = "head") -> None:
    command.upgrade(_alembic_config(name), revision)


def downgrade(name: str, revision: str) -> None:
    command.downgrade(_alembic_config(name), revision)


def current(name: str) -> None:
    command.current(_alembic_config(name), verbose=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="atelier 双库迁移")
    parser.add_argument("--db", choices=[*DB_NAMES, "all"], default="all")
    parser.add_argument("--revision", default="head")
    parser.add_argument("--action", choices=["upgrade", "downgrade", "current"], default="upgrade")
    args = parser.parse_args()

    targets = DB_NAMES if args.db == "all" else (args.db,)
    for name in targets:
        print(f"[{name}] {args.action} -> {args.revision}")
        if args.action == "upgrade":
            upgrade(name, args.revision)
        elif args.action == "downgrade":
            downgrade(name, args.revision)
        else:
            current(name)


if __name__ == "__main__":
    main()
