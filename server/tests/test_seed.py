"""seeds/ 灌入的幂等性，以及 seed 文件本身的自洽性。

灌库用内存库，不碰仓库里的 db/config.db。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from atelier.agents.definitions import CAPABILITIES
from atelier.db.config_models import AssetCategory, ConfigBase, ModelCatalog, WorkflowDef
from atelier.db.seed import _prune, _upsert
from atelier.settings import get_settings

MODEL_CATALOG_PK = ("vendor", "plan", "model_id")


def _seeds(name: str) -> list[dict[str, Any]]:
    path: Path = get_settings().seeds_dir / name
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return data


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://", future=True)
    ConfigBase.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_upsert_is_idempotent(session: Session) -> None:
    rows = _seeds("model_catalog.json")
    assert [_upsert(session, ModelCatalog, MODEL_CATALOG_PK, r) for r in rows] == ["created"] * len(
        rows
    )
    session.commit()

    assert [_upsert(session, ModelCatalog, MODEL_CATALOG_PK, r) for r in rows] == [
        "unchanged"
    ] * len(rows)
    session.commit()
    assert session.scalar(select(func.count()).select_from(ModelCatalog)) == len(rows)


def test_upsert_reports_updated_on_content_change(session: Session) -> None:
    row = dict(_seeds("model_catalog.json")[0])
    assert _upsert(session, ModelCatalog, MODEL_CATALOG_PK, row) == "created"
    session.commit()

    row["remark"] = "改了备注"
    assert _upsert(session, ModelCatalog, MODEL_CATALOG_PK, row) == "updated"
    session.commit()
    assert session.scalars(select(ModelCatalog.remark)).one() == "改了备注"


def test_prune_drops_records_missing_from_seeds(session: Session) -> None:
    rows = _seeds("asset_categories.json")
    for row in rows:
        _upsert(session, AssetCategory, ("code",), row)
    session.add(AssetCategory(code="stale", name="过期维度", dir_name="stale"))
    session.commit()

    assert _prune(session, AssetCategory, "code", {r["code"] for r in rows}) == 1
    session.commit()
    assert "stale" not in set(session.scalars(select(AssetCategory.code)))


def test_model_catalog_keys_are_unique() -> None:
    keys = [tuple(r[f] for f in MODEL_CATALOG_PK) for r in _seeds("model_catalog.json")]
    assert len(keys) == len(set(keys))


def test_model_catalog_capabilities_are_known() -> None:
    for row in _seeds("model_catalog.json"):
        assert row["capabilities"], row["model_id"]
        assert set(row["capabilities"]) <= CAPABILITIES, row["model_id"]


def test_model_catalog_carries_no_credentials() -> None:
    """候选清单进 Git，只能有端点与前缀提示，绝不能带 key。"""
    for row in _seeds("model_catalog.json"):
        assert "api_key" not in row
        assert row["base_url"].startswith("https://")


def test_asset_categories_and_workflows_have_character() -> None:
    assert "character" in {r["code"] for r in _seeds("asset_categories.json")}
    workflows = _seeds("workflow_defs.json")
    assert "character" in {r["target_kind"] for r in workflows}
    for row in workflows:
        assert row["states"] and row["transitions"]


def test_workflow_def_model_accepts_seed_rows(session: Session) -> None:
    for row in _seeds("workflow_defs.json"):
        assert _upsert(session, WorkflowDef, ("code",), row) == "created"
    session.commit()


def test_meshy_actions_seed_is_an_honest_placeholder() -> None:
    """600+ 动作 id 不臆造，C1 阶段从 Meshy API 拉取回写。"""
    raw = json.loads((get_settings().seeds_dir / "meshy_actions.json").read_text(encoding="utf-8"))
    assert raw["actions"] == []
    assert raw["synced_at"] is None
