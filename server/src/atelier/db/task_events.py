"""`task_events` 的写入口。

这张表是运行日志与门禁决策的单一时间线：会话沉淀、自动裁决、人工拍板都往里写，前端的日志
面板与 SSE 只读它。`seq` 按 `task_id` 单独递增而不是用自增主键——同一个素材的事件要能按 1、
2、3 讲成一件事，主键在多个素材交错写入时是跳号的。

`task_id` 不强制对应 `tasks` 行：会话用会话 id，门禁用素材 id。为一行门禁记录去建一个任务
行只会多一处要维护的状态。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from atelier.db.project_models import TaskEvent


def record(
    project: Session,
    task_id: str,
    event: str,
    message: str = "",
    payload: Mapping[str, Any] | None = None,
    *,
    level: str = "info",
) -> TaskEvent:
    """追加一条事件。不提交——由调用方跟自己的改动一起提交，免得日志比事实先落库。"""
    current = project.scalar(select(func.max(TaskEvent.seq)).where(TaskEvent.task_id == task_id))
    row = TaskEvent(
        task_id=task_id,
        seq=int(current or 0) + 1,
        level=level,
        event=event,
        message=message,
        payload=dict(payload or {}),
    )
    project.add(row)
    return row


def history(project: Session, task_id: str) -> list[TaskEvent]:
    """某个任务/素材的事件，按 seq 升序。"""
    return list(
        project.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.seq)
        )
    )
