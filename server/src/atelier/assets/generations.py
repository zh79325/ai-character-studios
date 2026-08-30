"""产物台账：每一张生成出来的图都在这里留一行。

为什么中间产物也登记：一次门禁上人要在几张候选之间挑，挑完之后还要能回答「被采用的那张
是哪次调用、用的什么 prompt 和参数」。图本身只是一堆字节，答不出这些；`tmp/` 里的文件名
也只能表达先后，不能表达出处。所以生成即登记，采用只是把其中一行标成定稿。

`is_final` 在同一个 (target, stage, variant) 里只允许一行为真——定稿是唯一的。换定稿时先
把旧的那行落回 false 而不是删掉：旧定稿的文件退位后还在 `tmp/` 里，台账跟着留住才对得上。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from atelier.db.project_models import Generation

_log = structlog.get_logger(__name__)

CHARACTER = "character"
"""`target_kind` 目前只有角色，但字段留着，装备/场景走同一张表。"""

RENDER = "render"
VIEWS = "views"


def record(
    project: Session,
    *,
    target_ref: str,
    stage: str,
    file_path: str,
    file_hash: str,
    target_kind: str = CHARACTER,
    variant: str | None = None,
    task_id: str | None = None,
    asset_spec: Mapping[str, Any] | None = None,
    source: str = "generated",
) -> Generation:
    """登记一条产物。不 commit，由调用方跟状态改动一起提交。

    刚生成出来的一律 `is_final=False`：产物落在 `tmp/` 里，定稿位上还是旧的那张，这时候标
    成定稿会让台账比磁盘超前一步。
    """
    row = Generation(
        id=uuid.uuid4().hex,
        target_kind=target_kind,
        target_ref=target_ref,
        stage=stage,
        variant=variant,
        file_path=file_path,
        file_hash=file_hash,
        is_final=False,
        source=source,
        task_id=task_id,
        asset_spec=dict(asset_spec or {}),
    )
    project.add(row)
    return row


def get(project: Session, generation_id: str) -> Generation | None:
    return project.get(Generation, generation_id)


def candidates(
    project: Session,
    *,
    target_ref: str,
    stage: str,
    target_kind: str = CHARACTER,
    variant: str | None = None,
) -> list[Generation]:
    """某一步的全部产物，新的在前——门禁上人先看最近一张。"""
    query = (
        select(Generation)
        .where(
            Generation.target_kind == target_kind,
            Generation.target_ref == target_ref,
            Generation.stage == stage,
        )
        .order_by(Generation.created_at.desc(), Generation.id.desc())
    )
    if variant is not None:
        query = query.where(Generation.variant == variant)
    return list(project.scalars(query))


def latest(
    project: Session,
    *,
    target_ref: str,
    stage: str,
    target_kind: str = CHARACTER,
    variant: str | None = None,
) -> Generation | None:
    rows = candidates(
        project, target_ref=target_ref, stage=stage, target_kind=target_kind, variant=variant
    )
    return rows[0] if rows else None


def final(
    project: Session,
    *,
    target_ref: str,
    stage: str,
    target_kind: str = CHARACTER,
    variant: str | None = None,
) -> Generation | None:
    for row in candidates(
        project, target_ref=target_ref, stage=stage, target_kind=target_kind, variant=variant
    ):
        if row.is_final:
            return row
    return None


def mark_final(
    project: Session, chosen: Generation, *, file_path: str, file_hash: str
) -> Generation:
    """把这一行标成定稿，同组里旧的定稿落回候选。

    `file_path` 与 `file_hash` 要重写：这行原先指向 `tmp/` 里的候选，采用之后定稿位上才是
    它真正的落点。指向 `tmp/` 的定稿行迟早会因为清理目录而悬空。
    """
    for row in candidates(
        project,
        target_ref=chosen.target_ref,
        stage=chosen.stage,
        target_kind=chosen.target_kind,
        variant=chosen.variant,
    ):
        if row.id != chosen.id and row.is_final:
            row.is_final = False
    chosen.file_path = file_path
    chosen.file_hash = file_hash
    chosen.is_final = True
    _log.info(
        "generation_final",
        id=chosen.id,
        target=chosen.target_ref,
        stage=chosen.stage,
        path=file_path,
    )
    return chosen
