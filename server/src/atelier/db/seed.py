"""配置库幂等灌入：seeds/ 是真相，config.db 是它的可查询副本。

    uv run atelier-seed                    # 灌全部
    uv run atelier-seed --only model_catalog
    uv run atelier-seed --prune            # 同时删掉 seeds 里已不存在的记录

提示词不走这里：工程级提示词是代码资产，住在 atelier/prompts/，运行时直读文件，
改完即生效，不入库也不必重跑本命令。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from atelier.settings import get_settings

from .config_models import AssetCategory, MeshyAction, ModelCatalog, WorkflowDef
from .session import config_session


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _upsert(
    session: Session, model: type[Any], pk_fields: Sequence[str], row: dict[str, Any]
) -> str:
    """按主键 upsert，返回 created / updated / unchanged。"""
    stmt = select(model)
    for f in pk_fields:
        stmt = stmt.where(getattr(model, f) == row[f])
    existing = session.scalars(stmt).one_or_none()

    if existing is None:
        session.add(model(**row))
        return "created"

    changed = False
    for key, value in row.items():
        if getattr(existing, key) != value:
            setattr(existing, key, value)
            changed = True
    return "updated" if changed else "unchanged"


def _prune(session: Session, model: type[Any], pk_field: str, keep: set[str]) -> int:
    """删掉 seeds 里已不存在的记录。"""
    col = getattr(model, pk_field)
    existing = set(session.scalars(select(col)).all())
    gone = existing - keep
    if not gone:
        return 0
    session.execute(delete(model).where(col.in_(gone)))
    return len(gone)


class SeedReport(dict[str, dict[str, int]]):
    def bump(self, table: str, outcome: str) -> None:
        self.setdefault(table, {"created": 0, "updated": 0, "unchanged": 0, "pruned": 0})
        self[table][outcome] += 1

    def render(self) -> str:
        lines = []
        for table in sorted(self):
            c = self[table]
            lines.append(
                f"  {table:<20} 新增 {c['created']:>4}  更新 {c['updated']:>4}  "
                f"不变 {c['unchanged']:>4}  删除 {c['pruned']:>4}"
            )
        return "\n".join(lines)


def _seed_json_table(
    session: Session,
    report: SeedReport,
    *,
    table: str,
    model: type[Any],
    pk_fields: Sequence[str],
    rows: list[dict[str, Any]] | None,
    prune: bool,
) -> None:
    if rows is None:
        return
    for row in rows:
        report.bump(table, _upsert(session, model, pk_fields, row))
    if prune and len(pk_fields) == 1:
        n = _prune(session, model, pk_fields[0], {str(r[pk_fields[0]]) for r in rows})
        for _ in range(n):
            report.bump(table, "pruned")


def seed_all(only: str | None = None, prune: bool = False) -> SeedReport:
    settings = get_settings()
    seeds = settings.seeds_dir
    report = SeedReport()

    meshy_raw = _load_json(seeds / "meshy_actions.json")
    meshy_rows = meshy_raw.get("actions") if isinstance(meshy_raw, dict) else meshy_raw

    plan: list[tuple[str, type[Any], Sequence[str], Any]] = [
        (
            "model_catalog",
            ModelCatalog,
            ("vendor", "plan", "model_id"),
            _load_json(seeds / "model_catalog.json"),
        ),
        ("meshy_actions", MeshyAction, ("action_id",), meshy_rows),
        ("asset_categories", AssetCategory, ("code",), _load_json(seeds / "asset_categories.json")),
        ("workflow_defs", WorkflowDef, ("code",), _load_json(seeds / "workflow_defs.json")),
    ]

    with config_session() as session:
        for table, model, pk_fields, rows in plan:
            if only not in (None, table):
                continue
            _seed_json_table(
                session,
                report,
                table=table,
                model=model,
                pk_fields=pk_fields,
                rows=rows,
                prune=prune,
            )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="把 seeds/ 幂等灌进配置库")
    parser.add_argument(
        "--only",
        choices=[
            "model_catalog",
            "meshy_actions",
            "asset_categories",
            "workflow_defs",
        ],
        default=None,
    )
    parser.add_argument("--prune", action="store_true", help="删掉 seeds 里已不存在的记录")
    args = parser.parse_args()

    report = seed_all(only=args.only, prune=args.prune)
    print(f"配置库 {get_settings().config_db_path}")
    print(report.render())


if __name__ == "__main__":
    main()
