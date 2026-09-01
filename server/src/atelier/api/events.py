"""SSE：路由决策日志与任务事件。

推送方式是「轮询数据库增量」而不是内存广播——写事件的可能是后台线程、也可能是另一个
进程（迁移、seed），只要它写进了库，面板就能看到，不需要谁记得去发广播。代价是最多迟
`POLL_SECONDS` 上屏，对日志面板足够。

两类事件住在不同的库里：路由日志是机器级的（全局 runtime.db），任务与任务事件是项目级的
（项目目录下的 project.db）。所以任务流同时拿两个 Session，各自轮询自己那份，不做 join。

事件里的内容在写库前已脱敏（key 只留前 4 后 4），这里原样转发，不再解析。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from sse_starlette import EventSourceResponse, ServerSentEvent

from atelier.api.deps import ProjectDb, RuntimeDb
from atelier.api.schemas import RouteLogOut
from atelier.db.project_models import Task, TaskEvent
from atelier.db.runtime_models import RouteLog

router = APIRouter(prefix="/api/events", tags=["events"])
task_router = APIRouter(prefix="/api/projects/{project_code}/events", tags=["events"])

POLL_SECONDS = 0.5
"""轮询间隔。日志面板不需要更快，太快只是在空转 SQLite。"""

BACKLOG = 200
"""不指定游标时先补多少条历史，让面板一打开就有内容。"""

BATCH = 200
PING_SECONDS = 15
TERMINAL_STATUS = ("succeeded", "failed", "cancelled")


def _route_log_out(row: RouteLog) -> RouteLogOut:
    return RouteLogOut(
        id=row.id,
        ts=row.ts.isoformat(),
        agent_code=row.agent_code,
        provider_code=row.provider_code,
        model_id=row.model_id,
        outcome=row.outcome,
        reason=row.reason,
        attempt_no=row.attempt_no,
        latency_ms=row.latency_ms,
        used_delta=row.used_delta,
        limit_kind=row.limit_kind,
        task_id=row.task_id,
        conversation_id=row.conversation_id,
        project_code=row.project_code,
    )


def fetch_route_logs(
    session: Session, after_id: int, *, task_id: str | None = None, limit: int = BATCH
) -> list[RouteLog]:
    stmt = select(RouteLog).where(RouteLog.id > after_id).order_by(RouteLog.id).limit(limit)
    if task_id is not None:
        stmt = stmt.where(RouteLog.task_id == task_id)
    return list(session.scalars(stmt).all())


def fetch_task_events(
    session: Session, task_id: str, after_seq: int, *, limit: int = BATCH
) -> list[TaskEvent]:
    return list(
        session.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.seq > after_seq)
            .order_by(TaskEvent.seq)
            .limit(limit)
        ).all()
    )


def _start_cursor(session: Session, after_id: int | None) -> int:
    """没给游标就从「最近 BACKLOG 条之前」开始，给了就听它的（0 = 从头看）。"""
    if after_id is not None:
        return max(after_id, 0)
    latest = session.scalars(select(RouteLog.id).order_by(RouteLog.id.desc()).limit(1)).first()
    return max((latest or 0) - BACKLOG, 0)


@router.get("/route-logs")
async def route_log_stream(
    request: Request,
    session: RuntimeDb,
    after_id: int | None = Query(
        default=None, description="从这个 id 之后开始推，不给则补最近若干条"
    ),
    agent_code: str | None = Query(default=None),
) -> EventSourceResponse:
    """选路决策实时流：谁被选中、谁被跳过、为什么。"""
    cursor = _start_cursor(session, after_id)

    async def stream() -> AsyncIterator[ServerSentEvent]:
        nonlocal cursor
        yield ServerSentEvent(event="ready", data=str(cursor))
        while not await request.is_disconnected():
            # 结束上一轮的只读事务，否则 SQLite 快照会一直看不到新写入
            session.rollback()
            for row in fetch_route_logs(session, cursor):
                cursor = row.id
                if agent_code and row.agent_code != agent_code:
                    continue
                yield ServerSentEvent(
                    event="route_log", id=str(row.id), data=_route_log_out(row).model_dump_json()
                )
            await asyncio.sleep(POLL_SECONDS)

    return EventSourceResponse(stream(), ping=PING_SECONDS)


@task_router.get("/{task_id}")
async def task_event_stream(
    request: Request,
    runtime: RuntimeDb,
    tasks: ProjectDb,
    task_id: str,
    after_seq: int = Query(default=0),
) -> EventSourceResponse:
    """单个任务的运行日志 + 它的路由决策；任务进终态后推 done 并收流。

    任务在项目库（`tasks`），路由日志在全局库（`runtime`），两边各自推进自己的游标。
    """
    task = tasks.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务 {task_id} 不存在")

    seq_cursor = after_seq
    log_cursor = 0

    async def stream() -> AsyncIterator[ServerSentEvent]:
        nonlocal seq_cursor, log_cursor
        yield ServerSentEvent(event="ready", data=task_id)
        while not await request.is_disconnected():
            tasks.rollback()
            runtime.rollback()
            fresh = False

            for row in fetch_task_events(tasks, task_id, seq_cursor):
                seq_cursor = row.seq
                fresh = True
                yield ServerSentEvent(
                    event="task_event",
                    id=f"{task_id}:{row.seq}",
                    data=_task_event_json(row),
                )

            for log in fetch_route_logs(runtime, log_cursor, task_id=task_id):
                log_cursor = log.id
                fresh = True
                yield ServerSentEvent(
                    event="route_log", id=str(log.id), data=_route_log_out(log).model_dump_json()
                )

            current = tasks.get(Task, task_id)
            if current is not None and current.status in TERMINAL_STATUS and not fresh:
                yield ServerSentEvent(event="done", data=current.status)
                return
            await asyncio.sleep(POLL_SECONDS)

    return EventSourceResponse(stream(), ping=PING_SECONDS)


def _task_event_json(row: TaskEvent) -> str:
    return json.dumps(
        {
            "task_id": row.task_id,
            "seq": row.seq,
            "ts": row.ts.isoformat(),
            "level": row.level,
            "event": row.event,
            "message": row.message,
            "payload": row.payload,
        },
        ensure_ascii=False,
    )
