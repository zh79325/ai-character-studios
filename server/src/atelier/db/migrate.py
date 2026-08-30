"""迁移入口。

全局两库（config / runtime）位置固定，一条命令升到 head：

    uv run atelier-migrate                      # 两库都升
    uv run atelier-migrate --db config
    uv run atelier-migrate --action downgrade --revision -1 --db runtime

项目库一个项目一个，位置由用户决定，所以必须点名路径：

    uv run atelier-migrate --db project --project-db /任意路径/项目名/.atelier/project.db

代码里新建或打开项目时走 `upgrade_project(db_path)`，它是幂等的：已经在 head 就什么都
不做，落后了就补齐。项目目录换机器、换版本后第一次打开就靠这个把库补到当前结构。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config

SERVER_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = SERVER_ROOT / "alembic.ini"
GLOBAL_DB_NAMES = ("config", "runtime")
PROJECT_DB_NAME = "project"


def _alembic_config(name: str, project_db: Path | None = None) -> Config:
    cfg = Config(str(ALEMBIC_INI), ini_section=name)
    # script_location 在 ini 里是相对 server/ 的路径，这里补成绝对路径，
    # 使得从任意 cwd 调用都能定位 version 目录。
    cfg.set_main_option("script_location", str(SERVER_ROOT / f"alembic/{name}"))
    if project_db is not None:
        # env.py 从 -x db=... 取库路径，这是它唯一的输入
        cfg.cmd_opts = argparse.Namespace(x=[f"db={Path(project_db).expanduser().resolve()}"])
    return cfg


def upgrade(name: str, revision: str = "head") -> None:
    command.upgrade(_alembic_config(name), revision)


def downgrade(name: str, revision: str) -> None:
    command.downgrade(_alembic_config(name), revision)


def current(name: str) -> None:
    command.current(_alembic_config(name), verbose=True)


def upgrade_project(db_path: Path, revision: str = "head") -> None:
    """把某个项目自己的库升到 head。新建项目时也是它负责建表。"""
    command.upgrade(_alembic_config(PROJECT_DB_NAME, db_path), revision)


def downgrade_project(db_path: Path, revision: str) -> None:
    command.downgrade(_alembic_config(PROJECT_DB_NAME, db_path), revision)


def main() -> None:
    parser = argparse.ArgumentParser(description="atelier 迁移：全局两库 + 每项目一库")
    parser.add_argument("--db", choices=[*GLOBAL_DB_NAMES, PROJECT_DB_NAME, "all"], default="all")
    parser.add_argument("--revision", default="head")
    parser.add_argument("--action", choices=["upgrade", "downgrade", "current"], default="upgrade")
    parser.add_argument(
        "--project-db",
        type=Path,
        default=None,
        help="项目库路径，--db project 时必填，如 /任意路径/项目名/.atelier/project.db",
    )
    args = parser.parse_args()

    if args.db == PROJECT_DB_NAME:
        if args.project_db is None:
            parser.error("--db project 必须同时给 --project-db")
        print(f"[project] {args.action} -> {args.revision}  ({args.project_db})")
        if args.action == "upgrade":
            upgrade_project(args.project_db, args.revision)
        elif args.action == "downgrade":
            downgrade_project(args.project_db, args.revision)
        else:
            command.current(_alembic_config(PROJECT_DB_NAME, args.project_db), verbose=True)
        return

    targets = GLOBAL_DB_NAMES if args.db == "all" else (args.db,)
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
