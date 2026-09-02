"""统一 Action 中素材规格的字段抽取与缺项检查。"""

from __future__ import annotations

from atelier.agents.parsing import parse_asset_specs
from tests.conftest import action_reply

ONE_SPEC = {
    "code": "ASSET-CT-001",
    "name": "赤瞳 渲染图",
    "category": "character",
    "size": "2048x2048",
    "format": "png",
    "file_name": "character_赤瞳_渲染图.png",
    "description": "一只双尾兽站在废弃电厂前，红瞳发光。",
    "anchors": "§1 冷调工业写实",
    "constraints": ["双尾数量=2", "瞳色=赤红"],
    "view_background_color": "#F2E8D5（暖米色）",
    "prompt": "standing pose, red eyes, TWO distinct tails, cinematic light, 8k",
    "negative_prompt": "background clutter, watermark, text",
}
ONE_CARD = action_reply("卡片已完成。", payload={"asset_specs": [ONE_SPEC]})


def test_一张卡片各字段都对得上() -> None:
    (spec,) = parse_asset_specs(ONE_CARD)

    assert spec.code == "ASSET-CT-001"
    assert spec.name == "赤瞳 渲染图"
    assert spec.category == "character"
    assert (spec.width, spec.height) == (2048, 2048)
    assert spec.image_format == "png"
    assert spec.file_name == "character_赤瞳_渲染图.png"
    assert spec.anchors.startswith("§1")
    assert spec.constraints == ("双尾数量=2", "瞳色=赤红")
    assert spec.negative_prompt == "background clutter, watermark, text"
    assert spec.gaps() == ()


def test_原文一并留着() -> None:
    (spec,) = parse_asset_specs(ONE_CARD)

    assert "ASSET-CT-001" in spec.text
    assert "negative_prompt" in spec.text
    assert spec.as_dict()["card"] == spec.text


def test_多张卡片各算一张() -> None:
    second = {
        **ONE_SPEC,
        "code": "ASSET-CT-002",
        "name": "赤瞳 正面视图",
        "file_name": "character_赤瞳_正面视图.png",
    }

    specs = parse_asset_specs(action_reply(payload={"asset_specs": [ONE_SPEC, second]}))

    assert [one.code for one in specs] == ["ASSET-CT-001", "ASSET-CT-002"]
    assert specs[1].file_name == "character_赤瞳_正面视图.png"


def test_没有卡片返回空() -> None:
    assert parse_asset_specs(action_reply("只说明处理结果。")) == ()


def test_缺项要报出来而不是给个空值() -> None:
    thin = {
        **ONE_SPEC,
        "size": "",
        "negative_prompt": "",
    }

    (spec,) = parse_asset_specs(action_reply(payload={"asset_specs": [thin]}))

    assert spec.gaps() == ("尺寸", "negative_prompt")


def test_文件名带路径也算缺项() -> None:
    nested = {**ONE_SPEC, "file_name": "images/character_赤瞳_渲染图.png"}

    (spec,) = parse_asset_specs(action_reply(payload={"asset_specs": [nested]}))

    assert spec.gaps() == ("文件名",)


def test_非角色卡片不强制四视图背景色() -> None:
    scene = {
        **ONE_SPEC,
        "code": "ASSET-SCENE-001",
        "category": "scene",
        "view_background_color": "",
    }

    (spec,) = parse_asset_specs(action_reply(payload={"asset_specs": [scene]}))

    assert "四视图背景色" not in spec.gaps()
