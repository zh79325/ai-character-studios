"""请求级依赖：双库 Session。

两个库各自一个 Session，路由函数按需取。写操作在请求结束时统一提交，出错回滚——
providers/router 与 providers/usage 内部会自行 commit，这里只兜住 API 层自己的改动。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from atelier.db.session import ConfigSession, RuntimeSession


def runtime_db() -> Iterator[Session]:
    session = RuntimeSession()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def config_db() -> Iterator[Session]:
    session = ConfigSession()
    try:
        yield session
    finally:
        session.close()


RuntimeDb = Annotated[Session, Depends(runtime_db)]
ConfigDb = Annotated[Session, Depends(config_db)]
