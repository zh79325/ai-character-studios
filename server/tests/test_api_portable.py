"""整包导入导出：吃得下别人的 provider_agents.json，且自家导出再导入无损。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from atelier.api.portable import build_portable, parse_portable

# 参考格式：只有 agents 单值映射 + model_limits，没有本工具的扩展键
REFERENCE: dict[str, Any] = {
    "ark_normal_wytn": {
        "priority": 5,
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": "sk-sp-abcdefgh12345678",
        "agents": {"spec_writer": "glm-5-2-260617", "vision_reviewer": "ep-2026-vision"},
        "model_limits": {
            "glm-5-2-260617": {
                "max_tokens": 1_800_000,
                "group": "glm5.2",
                "period": "day+11H",
            }
        },
        "remark": "方舟免费额度",
    },
    "goldbean": {
        "enabled": False,
        "base_url": "https://goldbean.invalid/v1",
        "api_key": "gb-key",
        "auth_style": "x-api-key",
        "verify_ssl": False,
        "agents": {"spec_reviewer": "gb-pro"},
        "key_source": "群里发的",
        "register_url": "https://goldbean.invalid/reg",
        "key_uses": 20,
    },
}


def test_reference_format_becomes_providers() -> None:
    providers, warnings = parse_portable(REFERENCE)

    ark = next(p for p in providers if p.code == "ark_normal_wytn")
    assert ark.priority == 5
    assert {m.model_id for m in ark.models} == {"glm-5-2-260617", "ep-2026-vision"}

    glm = next(m for m in ark.models if m.model_id == "glm-5-2-260617")
    assert glm.agents == ["spec_writer"]
    assert glm.limits[0].max_value == 1_800_000
    assert glm.limits[0].period_expr == "day+11H"
    assert glm.limits[0].group_name == "glm5.2"

    # 只在 agents 里出现、没配额度的模型也要建出来，否则绑定无处可挂
    vision = next(m for m in ark.models if m.model_id == "ep-2026-vision")
    assert vision.limits == []

    bean = next(p for p in providers if p.code == "goldbean")
    assert (bean.enabled, bean.auth_style, bean.verify_ssl) == (False, "x-api-key", False)
    assert any("key_source" in w for w in warnings)


def test_unsupported_fields_are_reported_not_swallowed() -> None:
    _, warnings = parse_portable(REFERENCE)
    ignored = next(w for w in warnings if w.startswith("goldbean"))
    assert "key_uses" in ignored and "register_url" in ignored


def test_broken_packages_are_rejected(client: TestClient) -> None:
    # 缺 base_url 是语义错，由 PortableError 担保；结构错在进函数之前就被 pydantic 拦下
    assert client.post("/api/providers/import", json={"providers": {"x": {}}}).status_code == 400
    assert (
        client.post("/api/providers/import", json={"providers": {"x": "字符串"}}).status_code == 422
    )


def test_import_merge_keeps_untouched_providers(client: TestClient) -> None:
    client.post(
        "/api/providers",
        json={"code": "mine", "base_url": "https://mine.invalid", "api_key": "sk-mine"},
    )

    result = client.post("/api/providers/import", json={"providers": REFERENCE}).json()
    assert sorted(result["created"]) == ["ark_normal_wytn", "goldbean"]
    assert result["updated"] == []
    assert result["models"] == 3
    assert result["bindings"] == 3
    assert result["limits"] == 1

    codes = [p["code"] for p in client.get("/api/providers").json()]
    assert "mine" in codes  # merge 不动没提到的账号


def test_import_replace_drops_the_rest(client: TestClient) -> None:
    client.post(
        "/api/providers",
        json={"code": "mine", "base_url": "https://mine.invalid", "api_key": "sk-mine"},
    )

    result = client.post(
        "/api/providers/import", json={"providers": REFERENCE, "mode": "replace"}
    ).json()
    assert result["removed"] == ["mine"]
    assert [p["code"] for p in client.get("/api/providers").json()] == [
        "ark_normal_wytn",
        "goldbean",
    ]


def test_reimport_updates_instead_of_duplicating(client: TestClient) -> None:
    client.post("/api/providers/import", json={"providers": REFERENCE})
    again = client.post("/api/providers/import", json={"providers": REFERENCE}).json()
    assert sorted(again["updated"]) == ["ark_normal_wytn", "goldbean"]
    assert again["created"] == []
    assert len(client.get("/api/providers/ark_normal_wytn").json()["models"]) == 2


def test_export_hides_keys_by_default(client: TestClient) -> None:
    client.post("/api/providers/import", json={"providers": REFERENCE})

    template = client.get("/api/providers/export")
    assert "abcdefgh12345678" not in template.text
    assert template.json()["ark_normal_wytn"]["api_key"] == ""

    full = client.get("/api/providers/export", params={"include_keys": True}).json()
    assert full["ark_normal_wytn"]["api_key"] == "sk-sp-abcdefgh12345678"


def test_template_import_does_not_wipe_local_keys(client: TestClient) -> None:
    """常见操作：把别人导出的模板导进来。此时不能把本机配好的 key 抹掉。"""
    client.post("/api/providers/import", json={"providers": REFERENCE})
    template = client.get("/api/providers/export").json()

    result = client.post("/api/providers/import", json={"providers": template}).json()
    assert client.get("/api/providers/ark_normal_wytn").json()["has_key"] is True
    assert result["warnings"] == []


def test_missing_key_anywhere_produces_a_warning(client: TestClient) -> None:
    package = {"blank": {"base_url": "https://blank.invalid", "agents": {}}}
    result = client.post("/api/providers/import", json={"providers": package}).json()
    assert any("补上" in w for w in result["warnings"])


def test_export_then_import_is_lossless(client: TestClient) -> None:
    """扩展键要能把参考格式装不下的信息带回来：模型级 driver、积分单价、一 Agent 多模型。"""
    rich = {
        "code": "meshy",
        "name": "Meshy",
        "base_url": "https://api.meshy.ai",
        "api_key": "msy-1234567890",
        "driver": "meshy",
        "models": [
            {
                "model_id": "meshy-5",
                "capabilities": ["model3d"],
                "driver": "meshy",
                "api_path": "/openapi/v1/image-to-3d",
                "params": {"credit_costs": {"image_to_3d": 5}},
                "agents": ["model3d_maker"],
                "limits": [{"limit_kind": "credits", "max_value": 1000, "period_expr": "total"}],
            },
            {
                "model_id": "meshy-4",
                "capabilities": ["model3d"],
                "sort_no": 1,
                "agents": ["model3d_maker"],
            },
        ],
    }
    client.post("/api/providers", json=rich)

    package = client.get("/api/providers/export", params={"include_keys": True}).json()
    # 参考格式只装得下一个模型，扩展键才是全量
    assert package["meshy"]["agents"]["model3d_maker"] == "meshy-5"
    assert package["meshy"]["agent_models"]["model3d_maker"] == ["meshy-5", "meshy-4"]

    client.post("/api/providers/import", json={"providers": package, "mode": "replace"})

    back = client.get("/api/providers/meshy").json()
    assert back["driver"] == "meshy"
    models = {m["model_id"]: m for m in back["models"]}
    assert models["meshy-5"]["params"]["credit_costs"]["image_to_3d"] == 5
    assert models["meshy-5"]["api_path"] == "/openapi/v1/image-to-3d"
    assert models["meshy-5"]["limits"][0]["period_expr"] == "total"
    assert models["meshy-4"]["agents"] == ["model3d_maker"]
    assert models["meshy-5"]["agents"] == ["model3d_maker"]


def test_build_portable_round_trips_through_the_parser() -> None:
    providers, _ = parse_portable(REFERENCE)
    package = build_portable(providers, include_keys=True)
    again, warnings = parse_portable(package)

    assert warnings == []
    assert [p.code for p in again] == [p.code for p in providers]
    assert [sorted(m.model_id for m in p.models) for p in again] == [
        sorted(m.model_id for m in p.models) for p in providers
    ]
