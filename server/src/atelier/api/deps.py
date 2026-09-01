"""请求级依赖：三个库的 Session。

配置库与全局日志库位置固定，各自一个 Session 工厂即可。项目库不同：它在项目目录下，
而项目目录可以在磁盘任意位置，所以要先定位「这次请求说的是哪个项目」，再开它的 Session。

定位规则：项目内接口的路径必须带 `project_code`。每次请求都按代号解析注册表、校验项目
目录，并保证项目库已升级到当前结构；不存在进程级「当前项目」或查询参数 fallback。

写操作在请求结束时统一提交，出错回滚——providers/router 与 providers/usage 内部会自行
commit，这里只兜住 API 层自己的改动。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Path
from sqlalchemy.orm import Session

from atelier.assets import projects
from atelier.assets.projects import ProjectRef
from atelier.db.session import ConfigSession, RuntimeSession, project_sessionmaker


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


def project_ref(
    runtime: RuntimeDb,
    project_code: Annotated[str, Path(description="项目 code")],
) -> ProjectRef:
    """按 URL 中的项目代号定位项目，并保证它的库已升到当前结构。"""
    ref = projects.resolve(runtime, project_code)
    projects.ensure_schema(ref)
    return ref


CurrentProject = Annotated[ProjectRef, Depends(project_ref)]


def project_db(ref: CurrentProject) -> Iterator[Session]:
    """指定项目库的 Session。项目代号来自 URL，不依赖任何进程级选择状态。"""
    session = project_sessionmaker(ref.db_path)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


ProjectDb = Annotated[Session, Depends(project_db)]
