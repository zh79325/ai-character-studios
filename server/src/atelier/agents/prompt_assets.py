"""工程级提示词资产加载：prompt 层模板与 negative 预设。

与 Agent 提示词同理，这两类也是代码资产，只住在 atelier/prompts/*.json，不入库、
不由 UI 修改。项目自己要加的片段在项目目录的 `prompts/snippets/*.md` 里，取用时与工程
预设合并——工程预设永远在前，项目片段追加在后，项目不能删工程预设。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from atelier.settings import get_settings

NEGATIVE_KIND = "negative"
LAYER_KIND = "layer"


class PromptAssetError(ValueError):
    """提示词资产文件不合规。"""


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """prompt 片段模板，由 prompt_smith 按 slot 拼装。"""

    code: str
    category: str
    slot: str
    content: str
    sort_no: int = 0
    remark: str | None = None


@dataclass(frozen=True, slots=True)
class NegativePreset:
    """全局 negative prompt 预设，与 art-bible 第 6 节合并后注入每次生图。"""

    code: str
    scene: str
    content: str
    remark: str | None = None


def _load_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise PromptAssetError(f"缺少提示词资产文件 {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise PromptAssetError(f"{path.name}: 顶层必须是数组")
    return data


@lru_cache
def load_prompt_templates() -> tuple[PromptTemplate, ...]:
    """按 slot 内 sort_no 升序返回全部层模板。"""
    rows = _load_json(get_settings().prompts_dir / "prompt_templates.json")
    templates = [PromptTemplate(**row) for row in rows]
    codes = [t.code for t in templates]
    dupes = {c for c in codes if codes.count(c) > 1}
    if dupes:
        raise PromptAssetError(f"prompt_templates.json: code 重复 {sorted(dupes)}")
    return tuple(sorted(templates, key=lambda t: (t.slot, t.sort_no, t.code)))


@lru_cache
def load_negative_presets() -> tuple[NegativePreset, ...]:
    rows = _load_json(get_settings().prompts_dir / "negative_presets.json")
    presets = [NegativePreset(**row) for row in rows]
    codes = [p.code for p in presets]
    dupes = {c for c in codes if codes.count(c) > 1}
    if dupes:
        raise PromptAssetError(f"negative_presets.json: code 重复 {sorted(dupes)}")
    return tuple(presets)


def templates_for_slot(slot: str, category: str | None = None) -> tuple[PromptTemplate, ...]:
    return tuple(
        t
        for t in load_prompt_templates()
        if t.slot == slot and (category is None or t.category == category)
    )


def negative_prompt(
    scene: str,
    art_bible_forbidden: str | None = None,
    project_snippets: list[str] | None = None,
) -> str:
    """拼出生图用的 negative_prompt。

    顺序固定：工程预设（通用 + 该场景）→ art bible 第 6 节 → 项目自定义片段。
    工程预设不可被项目覆盖或删除，只能被追加。
    """
    parts = [p.content for p in load_negative_presets() if p.scene in ("common", scene)]
    if art_bible_forbidden:
        parts.append(art_bible_forbidden)
    parts.extend(project_snippets or [])
    seen: set[str] = set()
    merged: list[str] = []
    for part in parts:
        for item in (s.strip() for s in part.split(",")):
            if item and item.lower() not in seen:
                seen.add(item.lower())
                merged.append(item)
    return ", ".join(merged)
