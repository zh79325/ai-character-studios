"""新建账号的预设：按套餐归组、端点与 driver 怎么定、哪些行不成预设。

预设只给表单初值，所以这里盯的是「归组正确、事实没走样」；真正落库仍是
`POST /api/providers` 那条路，由 test_api_providers 覆盖。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from atelier.db.config_models import ConfigBase, ModelCatalog
from atelier.providers import presets


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://", future=True)
    ConfigBase.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _row(**kwargs: object) -> ModelCatalog:
    base: dict[str, object] = {
        "vendor": "火山方舟",
        "plan": "Coding Plan",
        "preset_code": "ark-coding",
        "driver": "openai_compat",
        "model_id": "glm-5.3",
        "capabilities": ["text"],
        "limit_kind": "tokens",
        "default_period": "day+11H",
        "base_url": "https://ark.cn-beijing.volces.com",
        "api_path": "/api/coding/v3",
        "auth_style": "bearer",
        "key_prefix": None,
        "remark": None,
    }
    base.update(kwargs)
    return ModelCatalog(**base)


def test_one_plan_becomes_one_preset(session: Session) -> None:
    session.add_all([_row(), _row(model_id="deepseek-v4-pro")])
    session.commit()

    rows = presets.list_presets(session)
    assert len(rows) == 1

    one = rows[0]
    assert one.code == "ark-coding"
    assert one.label == "火山方舟 · Coding Plan"
    assert one.base_url == "https://ark.cn-beijing.volces.com"
    assert one.auth_style == "bearer"
    assert [m.model_id for m in one.models] == ["glm-5.3", "deepseek-v4-pro"]


def test_same_vendor_two_plans_stay_apart(session: Session) -> None:
    """两个套餐的 key 与端点完全隔离，混成一个账号会拿 Coding 的 key 去打 Agent 的端点。"""
    session.add_all(
        [
            _row(),
            _row(
                plan="Agent Plan",
                preset_code="ark-agent",
                driver="ark_image",
                model_id="doubao-seedream-5.0-lite",
                capabilities=["t2i", "i2i"],
                limit_kind="calls",
                api_path="/api/plan/v3/images/generations",
            ),
        ]
    )
    session.commit()

    rows = {one.code: one for one in presets.list_presets(session)}
    assert set(rows) == {"ark-coding", "ark-agent"}
    assert rows["ark-agent"].driver == "ark_image"
    assert rows["ark-agent"].models[0].limit_kind == "calls"


def test_each_model_carries_its_own_driver_and_path(session: Session) -> None:
    """一个套餐里文本与生图的 driver 常常不同，模型必须各带自己的，不能只继承 provider。"""
    session.add_all(
        [
            _row(preset_code="bailian-token", vendor="阿里百炼", plan="Token Plan 个人版"),
            _row(
                preset_code="bailian-token",
                vendor="阿里百炼",
                plan="Token Plan 个人版",
                driver="dashscope_mm",
                model_id="qwen-image-2.0",
                capabilities=["t2i", "i2i"],
                limit_kind="calls",
                api_path="/api/v1/services/aigc/multimodal-generation/generation",
            ),
        ]
    )
    session.commit()

    models = {m.model_id: m for m in presets.list_presets(session)[0].models}
    assert models["glm-5.3"].driver == "openai_compat"
    assert models["qwen-image-2.0"].driver == "dashscope_mm"
    assert models["qwen-image-2.0"].api_path.endswith("multimodal-generation/generation")


def test_provider_driver_takes_the_majority(session: Session) -> None:
    """provider 级 driver 只是「以后手工加模型」的默认值，取套餐里最常见的那个最省事。"""
    session.add_all(
        [
            _row(preset_code="ark-agent", plan="Agent Plan", driver="ark_video", model_id="v1"),
            _row(preset_code="ark-agent", plan="Agent Plan", driver="ark_video", model_id="v2"),
            _row(preset_code="ark-agent", plan="Agent Plan", driver="ark_image", model_id="i1"),
        ]
    )
    session.commit()

    assert presets.list_presets(session)[0].driver == "ark_video"


def test_key_prefix_travels_with_the_preset(session: Session) -> None:
    """填错 key（把按量的 sk- 填进 Token Plan）会静默走计费，所以前缀提示得跟着预设走。"""
    session.add(_row(preset_code="bailian-token", key_prefix="sk-sp-"))
    session.commit()

    assert presets.list_presets(session)[0].key_prefix == "sk-sp-"


def test_rows_without_preset_code_are_only_reference(session: Session) -> None:
    """速查表里的孤行凑不出一个能落库的账号，不该出现在新建下拉里。"""
    session.add_all([_row(), _row(preset_code="", model_id="随手记的一个型号")])
    session.commit()

    rows = presets.list_presets(session)
    assert [one.code for one in rows] == ["ark-coding"]
    assert [m.model_id for m in rows[0].models] == ["glm-5.3"]


def test_empty_catalog_gives_no_presets(session: Session) -> None:
    assert presets.list_presets(session) == []
