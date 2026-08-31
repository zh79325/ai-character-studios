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
from atelier.db.runtime_models import Provider, ProviderModel, RuntimeBase
from atelier.db.seed import SeedReport, _backfill_provider_models, _prune, _upsert
from atelier.providers import period
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


@pytest.fixture
def runtime() -> Iterator[Session]:
    engine = create_engine("sqlite://", future=True)
    RuntimeBase.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Provider(code="ark", name="方舟", base_url="https://ark.example.com", driver="ark"))
        s.commit()
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


def test_every_catalog_row_belongs_to_a_preset() -> None:
    """没归组的行进不了新建账号的下拉，用户就只能手工抄一遍端点与 driver。"""
    for row in _seeds("model_catalog.json"):
        assert row["preset_code"], row["model_id"]
        assert row["limit_kind"] in {"tokens", "calls", "credits"}, row["model_id"]
        assert period.normalize(row["default_period"]) == row["default_period"], row["model_id"]


def test_one_preset_means_one_endpoint_and_one_key() -> None:
    """同一预设内 base_url 与 key 前缀必须一致。

    套餐不同就是两个账号（方舟 Coding / Agent 的 key 不通用），归错组会拿一把 key
    去打另一个端点，而那条路往往不报错只是不走套餐额度、另行计费。
    """
    by_preset: dict[str, list[dict[str, Any]]] = {}
    for row in _seeds("model_catalog.json"):
        by_preset.setdefault(row["preset_code"], []).append(row)

    for code, rows in by_preset.items():
        assert len({r["base_url"] for r in rows}) == 1, code
        assert len({r["key_prefix"] for r in rows}) == 1, code
        assert len({(r["vendor"], r["plan"]) for r in rows}) == 1, code


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


def test_能聊天的型号都标了上下文窗口() -> None:
    """窗口缺了不报错，只是默默回落到 Agent 的保守预算，没人发现。"""
    for row in _seeds("model_catalog.json"):
        if not {"text", "vision"} & set(row["capabilities"]):
            continue
        window = (row.get("params") or {}).get("context_window")
        assert isinstance(window, int) and window > 0, row["model_id"]


def test_回补只补缺的那一项(session: Session, runtime: Session) -> None:
    for row in _seeds("model_catalog.json"):
        _upsert(session, ModelCatalog, MODEL_CATALOG_PK, row)
    session.commit()

    runtime.add_all(
        [
            ProviderModel(provider_code="ark", model_id="glm-5.3", capabilities=["text"]),
            ProviderModel(
                provider_code="ark",
                model_id="glm-5.3-flash",
                capabilities=["text"],
                params={"context_window": 200, "temperature": 0.3},
            ),
            ProviderModel(provider_code="ark", model_id="自己接的型号", capabilities=["text"]),
        ]
    )
    runtime.commit()

    report = SeedReport()
    catalog = {
        row["model_id"]: row["params"]
        for row in _seeds("model_catalog.json")
        if isinstance(row.get("params"), dict) and row["params"]
    }
    _backfill_provider_models(runtime, report, catalog)
    runtime.commit()

    by_id = {m.model_id: m for m in runtime.scalars(select(ProviderModel)).all()}
    assert by_id["glm-5.3"].params["context_window"] == 1000000
    # 设置页改过的数字比 seeds 更懂自己的账号，不能被盖
    assert by_id["glm-5.3-flash"].params == {"context_window": 200, "temperature": 0.3}
    # 目录里没这个型号，不臆造
    assert by_id["自己接的型号"].params == {}
    assert report["provider_models"]["updated"] == 1
    assert report["provider_models"]["unchanged"] == 1
