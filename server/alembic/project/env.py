"""项目库 Alembic 环境。

与另两个库不同：项目库不是全局唯一的一个文件，而是「每个项目目录下一个」，位置由用户
决定。所以库路径不能从 settings 拿，必须调用时给：

    alembic -n project -x db=/任意路径/项目名/.atelier/project.db upgrade head
    ATELIER_PROJECT_DB=/任意路径/项目名/.atelier/project.db alembic -n project upgrade head

代码里一般不直接调 alembic，走 `atelier.db.migrate.upgrade_project(db_path)`。
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context
from atelier.db.project_models import ProjectBase

config = context.config


def _db_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    raw = x_args.get("db") or os.environ.get("ATELIER_PROJECT_DB")
    if not raw:
        raise SystemExit(
            "项目库迁移必须指定库路径：alembic -n project -x db=/项目目录/.atelier/project.db ..."
        )
    path = Path(raw).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


url = _db_url()
config.set_main_option("sqlalchemy.url", url)

target_metadata = ProjectBase.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
