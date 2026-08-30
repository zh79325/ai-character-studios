"""只读元数据接口：下拉框取值域、配置库清单、代码资产里的 Agent 与提示词。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from atelier.db.config_models import AssetCategory, MeshyAction, ModelCatalog, WorkflowDef


def test_options_cover_every_dropdown(client: TestClient) -> None:
    """前端不许各自硬编码一份取值域，全从这里取。"""
    body = client.get("/api/config/options").json()

    assert "openai_compat" in body["drivers"] and "meshy" in body["drivers"]
    assert body["limit_kinds"] == ["tokens", "calls", "credits"]
    assert body["auth_styles"] == ["bearer", "x-api-key"]
    assert {"day", "month", "hour", "week", "total"} <= set(body["period_units"])
    assert "11 点" in body["period_examples"]["day+11H"]


def test_agents_come_from_markdown_not_the_database(client: TestClient) -> None:
    rows = client.get("/api/config/agents").json()
    codes = [row["agent_code"] for row in rows]

    assert codes == sorted(codes)
    assert {"game_designer", "spec_writer", "spec_reviewer", "prompt_smith"} <= set(codes)

    writer = next(row for row in rows if row["agent_code"] == "spec_writer")
    assert writer["source_file"] == "spec_writer.md"
    assert writer["conversational"] is True
    assert writer["system_prompt"] is None  # 默认不带，提示词很长


def test_agent_prompt_is_opt_in(client: TestClient) -> None:
    rows = client.get("/api/config/agents", params={"include_prompt": True}).json()
    writer = next(row for row in rows if row["agent_code"] == "spec_writer")
    assert writer["system_prompt"].startswith("你是")


def test_prompt_assets_are_readable(client: TestClient) -> None:
    templates = client.get("/api/config/prompt-templates").json()
    assert templates
    slots = {row["slot"] for row in templates}

    picked = client.get("/api/config/prompt-templates", params={"slot": sorted(slots)[0]}).json()
    assert {row["slot"] for row in picked} == {sorted(slots)[0]}

    presets = client.get("/api/config/negative-presets").json()
    assert any(row["scene"] == "common" for row in presets)


def test_model_catalog_filters(client: TestClient, cfg_session: Session) -> None:
    cfg_session.add_all(
        [
            ModelCatalog(
                vendor="volcengine",
                plan="coding",
                driver="openai_compat",
                model_id="glm-5.3",
                capabilities=["text"],
            ),
            ModelCatalog(
                vendor="volcengine",
                plan="agent",
                driver="ark_image",
                model_id="doubao-seedream-5.0",
                capabilities=["t2i", "i2i"],
            ),
            ModelCatalog(
                vendor="aliyun",
                plan="token",
                driver="dashscope_async",
                model_id="qwen-image",
                capabilities=["t2i"],
            ),
        ]
    )
    cfg_session.commit()

    assert len(client.get("/api/config/model-catalog").json()) == 3

    by_vendor = client.get("/api/config/model-catalog", params={"vendor": "aliyun"}).json()
    assert [row["model_id"] for row in by_vendor] == ["qwen-image"]

    by_cap = client.get("/api/config/model-catalog", params={"capability": "i2i"}).json()
    assert [row["model_id"] for row in by_cap] == ["doubao-seedream-5.0"]


def test_asset_categories_keep_their_order(client: TestClient, cfg_session: Session) -> None:
    cfg_session.add_all(
        [
            AssetCategory(code="scene", name="场景", dir_name="scenes", sort_no=2),
            AssetCategory(code="character", name="人物", dir_name="characters", sort_no=1),
        ]
    )
    cfg_session.commit()

    rows = client.get("/api/config/asset-categories").json()
    assert [row["code"] for row in rows] == ["character", "scene"]


def test_workflows_expose_their_states(client: TestClient, cfg_session: Session) -> None:
    cfg_session.add(
        WorkflowDef(
            code="character_v1",
            target_kind="character",
            name="人物流水线",
            states=["drafting", "spec_review", "rendering", "done"],
        )
    )
    cfg_session.commit()

    row = client.get("/api/config/workflows").json()[0]
    assert row["states"][0] == "drafting"


def test_meshy_actions_search(client: TestClient, cfg_session: Session) -> None:
    cfg_session.add_all(
        [
            MeshyAction(action_id="walk_01", name="Walking Forward", category="locomotion"),
            MeshyAction(action_id="idle_01", name="Idle Breathing", category="idle"),
        ]
    )
    cfg_session.commit()

    assert len(client.get("/api/config/meshy-actions").json()) == 2

    hit = client.get("/api/config/meshy-actions", params={"q": "walk"}).json()
    assert [row["action_id"] for row in hit] == ["walk_01"]

    by_category = client.get("/api/config/meshy-actions", params={"category": "idle"}).json()
    assert [row["action_id"] for row in by_category] == ["idle_01"]


def test_health_tells_where_the_databases_are(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["config_db"].endswith(".db")
    assert body["runtime_db"].endswith(".db")
