"""provider 设置页接口：增删改、脱敏、Agent 绑定、额度、看板、运行态复位。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from atelier.db.runtime_models import ProviderModel
from atelier.providers import router as provider_router
from atelier.providers import usage
from tests.conftest import make_model, make_provider

ARK = {
    "code": "ark_normal",
    "name": "方舟按量",
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "api_key": "sk-sp-1234567890abcdef",
    "priority": 10,
    "models": [
        {
            "model_id": "glm-5.3",
            "capabilities": ["text"],
            "agents": ["spec_writer", "spec_reviewer"],
            "limits": [
                {
                    "limit_kind": "tokens",
                    "max_value": 1_800_000,
                    "group_name": "glm5.3",
                    "period_expr": "day+11H",
                }
            ],
        }
    ],
}


def test_create_read_and_list(client: TestClient) -> None:
    created = client.post("/api/providers", json=ARK)
    assert created.status_code == 201

    body = created.json()
    assert body["code"] == "ark_normal"
    assert body["models"][0]["agents"] == ["spec_reviewer", "spec_writer"]
    assert body["models"][0]["limits"][0]["period_expr"] == "day+11H"
    assert body["models"][0]["endpoint"] == ARK["base_url"]

    listed = client.get("/api/providers").json()
    assert [p["code"] for p in listed] == ["ark_normal"]
    assert client.get("/api/providers/ark_normal").json()["name"] == "方舟按量"


def test_response_never_leaks_the_api_key(client: TestClient) -> None:
    """明文 key 只进库，不出接口——设置页显示掩码就够了。"""
    client.post("/api/providers", json=ARK)

    for url in ("/api/providers", "/api/providers/ark_normal", "/api/providers/usage"):
        text = client.get(url).text
        assert "1234567890abcdef" not in text

    body = client.get("/api/providers/ark_normal").json()
    assert body["api_key_mask"] == "sk-s***cdef"
    assert body["has_key"] is True


def test_duplicate_code_is_a_conflict(client: TestClient) -> None:
    client.post("/api/providers", json=ARK)
    assert client.post("/api/providers", json=ARK).status_code == 409


def test_unknown_provider_is_404(client: TestClient) -> None:
    assert client.get("/api/providers/nobody").status_code == 404
    assert client.patch("/api/providers/nobody", json={"enabled": False}).status_code == 404


def test_illegal_period_is_rejected(client: TestClient) -> None:
    """窗口语法必须与远程用量服务同源，写错要当场拦住而不是等到扣额度时才发现。"""
    payload = {
        **ARK,
        "models": [
            {
                "model_id": "glm-5.3",
                "limits": [{"limit_kind": "tokens", "max_value": 100, "period_expr": "month+11H"}],
            }
        ],
    }
    assert client.post("/api/providers", json=payload).status_code == 422


def test_unknown_driver_is_rejected(client: TestClient) -> None:
    assert client.post("/api/providers", json={**ARK, "driver": "手写的"}).status_code == 422


def test_patch_only_touches_given_fields(client: TestClient) -> None:
    client.post("/api/providers", json=ARK)

    patched = client.patch("/api/providers/ark_normal", json={"enabled": False}).json()
    assert patched["enabled"] is False
    assert patched["priority"] == 10
    assert patched["has_key"] is True  # 没传 api_key 就不动它


def test_delete_takes_models_and_limits_along(client: TestClient, session: Session) -> None:
    model_id = client.post("/api/providers", json=ARK).json()["models"][0]["id"]

    assert client.delete("/api/providers/ark_normal").status_code == 204
    assert client.get("/api/providers").json() == []
    assert session.get(ProviderModel, model_id) is None  # 外键级联带走模型与额度


def test_add_update_and_delete_a_model(client: TestClient) -> None:
    client.post("/api/providers", json=ARK)

    added = client.post(
        "/api/providers/ark_normal/models",
        json={
            "model_id": "doubao-seedream-5.0",
            "capabilities": ["t2i"],
            "driver": "ark_image",
            "api_path": "/images/generations",
            "agents": ["image_t2i"],
            "limits": [{"limit_kind": "calls", "max_value": 500, "period_expr": "day"}],
        },
    )
    assert added.status_code == 201
    model = added.json()
    assert model["effective_driver"] == "ark_image"
    assert model["endpoint"].endswith("/images/generations")

    updated = client.put(
        f"/api/providers/ark_normal/models/{model['id']}",
        json={"model_id": "doubao-seedream-5.0", "capabilities": ["t2i"], "agents": []},
    )
    assert updated.status_code == 200
    assert updated.json()["agents"] == []
    assert updated.json()["limits"] == []  # 整组替换：没传就是取消额度

    assert client.delete(f"/api/providers/ark_normal/models/{model['id']}").status_code == 204
    assert len(client.get("/api/providers/ark_normal").json()["models"]) == 1


def test_posting_an_existing_model_updates_it(client: TestClient) -> None:
    """设置页反复保存同一条是常态，不该报冲突。"""
    client.post("/api/providers", json=ARK)
    again = client.post(
        "/api/providers/ark_normal/models",
        json={"model_id": "glm-5.3", "sort_no": 7, "agents": ["spec_writer"]},
    )
    assert again.status_code == 201
    assert again.json()["sort_no"] == 7
    assert len(client.get("/api/providers/ark_normal").json()["models"]) == 1


def test_renaming_into_an_existing_model_is_a_conflict(client: TestClient) -> None:
    client.post("/api/providers", json=ARK)
    other = client.post("/api/providers/ark_normal/models", json={"model_id": "minimax-m3"}).json()

    clash = client.put(
        f"/api/providers/ark_normal/models/{other['id']}", json={"model_id": "glm-5.3"}
    )
    assert clash.status_code == 409


def test_model_of_another_provider_is_404(client: TestClient) -> None:
    client.post("/api/providers", json=ARK)
    other = client.post(
        "/api/providers", json={**ARK, "code": "bailian", "models": [{"model_id": "qwen3.8"}]}
    ).json()
    stranger = other["models"][0]["id"]

    assert client.delete(f"/api/providers/ark_normal/models/{stranger}").status_code == 404


def test_rebinding_agents_replaces_the_whole_set(client: TestClient) -> None:
    created = client.post("/api/providers", json=ARK).json()
    model_id = created["models"][0]["id"]

    rebound = client.put(
        f"/api/providers/ark_normal/models/{model_id}/agents", json=["prompt_smith"]
    )
    assert rebound.status_code == 200
    assert rebound.json()["agents"] == ["prompt_smith"]


def test_credit_costs_survive_a_round_trip(client: TestClient) -> None:
    """Meshy 的每种操作单价存在 params.credit_costs，配完要能读回来。"""
    payload = {
        "code": "meshy",
        "name": "Meshy",
        "base_url": "https://api.meshy.ai",
        "api_key": "msy_secret_key",
        "driver": "meshy",
        "models": [
            {
                "model_id": "meshy-5",
                "capabilities": ["model3d"],
                "driver": "meshy",
                "params": {"credit_costs": {"image_to_3d": 5, "animate": 10}},
                "limits": [{"limit_kind": "credits", "max_value": 1000, "period_expr": "total"}],
            }
        ],
    }
    created = client.post("/api/providers", json=payload).json()
    model = created["models"][0]
    assert model["params"]["credit_costs"]["image_to_3d"] == 5
    assert model["limits"][0]["window_text"] == "累计"


# --------------------------------------------------------------------------- #
# 额度看板
# --------------------------------------------------------------------------- #


def test_usage_board_shows_the_current_window(client: TestClient, session: Session) -> None:
    provider = make_provider(session, "ark", priority=5)
    model = make_model(
        session, provider, "glm-5.3", agent_code="spec_writer", limit=("tokens", 1000, "day")
    )
    usage.record(session, model, "tokens", 250)

    board = client.get("/api/providers/usage").json()
    assert board["limit_kinds"] == ["tokens", "calls", "credits"]

    item = board["items"][0]
    assert item["provider_code"] == "ark"
    assert item["agents"] == ["spec_writer"]
    assert item["breaker"] is None
    budget = item["budgets"][0]
    assert (budget["used"], budget["limit"], budget["available"]) == (250, 1000, 750)
    assert budget["window_text"] == "今日"
    assert budget["exhausted"] is False


def test_board_flags_missing_key_and_open_breaker(client: TestClient, session: Session) -> None:
    """缺 key 与正在熔断都要在看板上看得见，否则「为什么不走这个」无从判断。"""
    provider = make_provider(session, "ark", api_key="")
    model = make_model(session, provider, "glm-5.3")
    provider_router.open_breaker(session, model.id, "连不上")

    item = client.get("/api/providers/usage").json()["items"][0]
    assert item["has_key"] is False
    assert item["breaker"]["last_reason"] == "连不上"
    assert item["breaker"]["fail_count"] == 1


def test_clear_breaker_and_reset_usage(client: TestClient, session: Session) -> None:
    provider = make_provider(session, "ark")
    model = make_model(session, provider, "glm-5.3", limit=("tokens", 1000, "day"))
    provider_router.open_breaker(session, model.id, "偶发 500")
    usage.record(session, model, "tokens", 300)

    assert client.delete(f"/api/providers/ark/models/{model.id}/breaker").status_code == 204
    assert not provider_router.is_open(session, model.id)

    cleared = client.delete(f"/api/providers/ark/models/{model.id}/usage")
    assert cleared.json() == {"cleared": 1}
    assert usage.peek(session, model, "tokens").used == 0
