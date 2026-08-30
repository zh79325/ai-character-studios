"""请求级依赖：三个库的 Session。

配置库与全局日志库位置固定，各自一个 Session 工厂即可。项目库不同：它在项目目录下，
而项目目录可以在磁盘任意位置，所以要先定位「这次请求说的是哪个项目」，再开它的 Session。

定位规则：查询参数 `?project=code` 优先（前端切页时显式带上，避免和「当前项目」抢），
没给就用本机记住的当前项目。都没有就 404，让前端引导用户先建或导入一个项目。

写操作在请求结束时统一提交，出错回滚——providers/router 与 providers/usage 内部会自行
commit，这里只兜住 API 层自己的改动。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from atelier.assets import projects
from atelier.assets.projects import ProjectRef
from atelier.db.session import ConfigSession, RuntimeSession, project_sessionmaker
from atelier.errors import NotFound


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
    project: Annotated[str | None, Query(description="项目 code，省略则用当前项目")] = None,
) -> ProjectRef:
    """定位本次请求作用于哪个项目，并保证它的库已升到当前结构。

    只定位不切换：带 `?project=` 查一眼别的项目，不该把用户的当前项目换掉——换项目是
    `PUT /api/projects/current` 一个明确的动作。
    """
    ref = projects.resolve(runtime, project) if project else projects.current(runtime)
    if ref is None:
        raise NotFound("还没有选择项目，先新建或导入一个")
    projects.ensure_schema(ref)
    return ref


CurrentProject = Annotated[ProjectRef, Depends(project_ref)]


def project_db(ref: CurrentProject) -> Iterator[Session]:
    """当前项目库的 Session。切项目就是换一个库文件，数据隔离是天然的。"""
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
