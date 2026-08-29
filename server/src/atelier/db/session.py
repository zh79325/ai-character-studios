"""双库 session 工厂：各自 engine 与 Base，禁止跨库 join。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from atelier.settings import get_settings

from .config_models import ConfigBase
from .runtime_models import RuntimeBase


def _make_engine(url: str) -> Engine:
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


_settings = get_settings()

config_engine: Engine = _make_engine(_settings.config_db_url())
runtime_engine: Engine = _make_engine(_settings.runtime_db_url())

ConfigSession = sessionmaker(bind=config_engine, class_=Session, expire_on_commit=False)
RuntimeSession = sessionmaker(bind=runtime_engine, class_=Session, expire_on_commit=False)


@contextmanager
def config_session() -> Iterator[Session]:
    """配置库会话，出错回滚。"""
    with ConfigSession() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


@contextmanager
def runtime_session() -> Iterator[Session]:
    """日志库会话，出错回滚。"""
    with RuntimeSession() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def create_all_for_tests() -> None:
    """仅测试用：绕过 Alembic 直接建表。生产一律走 alembic upgrade head。"""
    ConfigBase.metadata.create_all(config_engine)
    RuntimeBase.metadata.create_all(runtime_engine)
