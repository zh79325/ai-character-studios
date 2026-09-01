"""工程级提示词资产：层模板与 negative 预设的加载与合并规则。

重点验证「工程预设不可被项目覆盖或删除，只能被追加」这条约束。
"""

from __future__ import annotations

from atelier.agents.prompt_assets import (
    load_negative_presets,
    load_prompt_templates,
    negative_prompt,
    templates_for_slot,
)


def test_templates_sorted_within_slot() -> None:
    templates = load_prompt_templates()
    assert templates
    keys = [(t.slot, t.sort_no, t.code) for t in templates]
    assert keys == sorted(keys)


def test_templates_for_slot_filters() -> None:
    pose = templates_for_slot("pose")
    assert pose
    assert {t.slot for t in pose} == {"pose"}
    assert templates_for_slot("pose", category="no_such_category") == ()


def test_negative_presets_have_common_scene() -> None:
    scenes = {p.scene for p in load_negative_presets()}
    assert "common" in scenes
    assert {"render", "views"} <= scenes


def test_negative_prompt_keeps_engine_presets_first() -> None:
    common = next(p for p in load_negative_presets() if p.scene == "common")
    merged = negative_prompt("views", project_snippets=["项目自定义词"])
    first_common_item = common.content.split(",")[0].strip()
    assert merged.startswith(first_common_item)
    assert merged.endswith("项目自定义词")


def test_negative_prompt_includes_scene_and_art_bible() -> None:
    views = next(p for p in load_negative_presets() if p.scene == "views")
    marker = views.content.split(",")[0].strip()
    merged = negative_prompt("views", art_bible_forbidden="禁止机械外壳")
    assert marker in merged
    assert "禁止机械外壳" in merged


def test_character_images_forbid_capes_and_flowing_garments() -> None:
    for scene in ("render", "views"):
        merged = negative_prompt(scene)
        assert "cape" in merged
        assert "cloak" in merged
        assert "long robe" in merged
        assert "loose flowing cloth" in merged


def test_negative_prompt_ignores_other_scenes() -> None:
    render_only = next(p for p in load_negative_presets() if p.scene == "render")
    marker = render_only.content.split(",")[0].strip()
    assert marker not in negative_prompt("views")


def test_negative_prompt_dedupes_case_insensitively() -> None:
    merged = negative_prompt("views", project_snippets=["BLURRY, 独有词, 独有词"])
    items = [s.strip() for s in merged.split(",")]
    lowered = [s.lower() for s in items]
    assert len(lowered) == len(set(lowered))
    assert items.count("独有词") == 1
    assert "BLURRY" not in items


def test_project_snippets_cannot_remove_engine_presets() -> None:
    """项目片段只能追加；不给片段与给片段时，工程预设部分必须完全一致。"""
    base = negative_prompt("views")
    extended = negative_prompt("views", project_snippets=["额外禁止项"])
    assert extended.startswith(base)
