"""三层 session 工厂：配置库、全局日志库，以及每个项目自带的项目库。

- 配置库 `db/config.db`：进 Git 的公共资产，只读为主。
- 全局日志库 `db/runtime.db`：本机的 provider 凭证、额度用量、路由日志、项目注册表。
- 项目库 `{项目目录}/.atelier/project.db`：项目自己的素材、任务、会话、记忆。项目目录
  可以在磁盘任意位置，所以它的 engine 不能在导入时建好，只能按路径动态开、并缓存住。

三者各自 engine 与 Base，禁止跨库 join。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from atelier.settings import get_settings

from .config_models import ConfigBase
from .project_models import ProjectBase
from .runtime_models import RuntimeBase


def _make_engine(url: str) -> Engine:
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        # SSE 长连接一边读一边写，写者撞上读者时等一会儿，别直接抛 database is locked
        cur.execute("PRAGMA busy_timeout=5000")
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
    """全局日志库会话，出错回滚。"""
    with RuntimeSession() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


# --------------------------------------------------------------------------- #
# 项目库：一个项目一个 engine，按库文件路径缓存
# --------------------------------------------------------------------------- #

_project_engines: dict[Path, Engine] = {}
_project_lock = threading.Lock()
"""engine 建一次能复用连接池，但同一路径不能建两个（各自一个 WAL 写者容易撞锁）。
API 是多线程的，所以缓存的写入要加锁。"""


def project_engine(db_path: Path) -> Engine:
    """取（或建）某个项目库的 engine。路径不存在时 SQLite 会建空文件，建表交给迁移。"""
    key = Path(db_path).resolve()
    engine = _project_engines.get(key)
    if engine is not None:
        return engine
    with _project_lock:
        engine = _project_engines.get(key)
        if engine is None:
            key.parent.mkdir(parents=True, exist_ok=True)
            engine = _make_engine(f"sqlite:///{key}")
            _project_engines[key] = engine
    return engine


def project_sessionmaker(db_path: Path) -> sessionmaker[Session]:
    return sessionmaker(bind=project_engine(db_path), class_=Session, expire_on_commit=False)


@contextmanager
def project_session(db_path: Path) -> Iterator[Session]:
    """项目库会话，出错回滚。"""
    with project_sessionmaker(db_path)() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def dispose_project_engine(db_path: Path) -> None:
    """放开某个项目库的连接与 WAL 文件句柄。

    项目目录要被移出、重命名或换盘时先调它，否则库文件被占着，用户在 Finder 里搬目录
    会留下 `-wal` / `-shm` 残骸。
    """
    key = Path(db_path).resolve()
    with _project_lock:
        engine = _project_engines.pop(key, None)
    if engine is not None:
        engine.dispose()


def dispose_project_engines() -> None:
    """进程收摊时统一释放。"""
    with _project_lock:
        engines = list(_project_engines.values())
        _project_engines.clear()
    for engine in engines:
        engine.dispose()


def create_all_for_tests(project_db: Path | None = None) -> None:
    """仅测试用：绕过 Alembic 直接建表。生产一律走 alembic upgrade head。"""
    ConfigBase.metadata.create_all(config_engine)
    RuntimeBase.metadata.create_all(runtime_engine)
    if project_db is not None:
        ProjectBase.metadata.create_all(project_engine(project_db))
