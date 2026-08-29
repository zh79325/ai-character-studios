"""配置库 Alembic 环境。url 与 metadata 都从 atelier 取，避免 ini 里写死路径。"""

from __future__ import annotations

from sqlalchemy import engine_from_config, pool

from alembic import context
from atelier.db.config_models import ConfigBase
from atelier.settings import get_settings

config = context.config
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.config_db_url())

target_metadata = ConfigBase.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.config_db_url(),
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
