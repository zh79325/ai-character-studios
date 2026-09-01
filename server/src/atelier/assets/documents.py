"""素材定稿提示词文档。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from atelier.assets import archive, layout
from atelier.assets.projects import ProjectRef


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _display(value: object) -> str:
    if value is None or value == "":
        return "（无）"
    return str(value)


def prompt_markdown(
    *,
    title: str,
    generation_id: str,
    final_path: str,
    asset_spec: Mapping[str, Any],
) -> str:
    """把定稿 generation 的完整生图输入整理成可读 Markdown。"""
    spec = dict(asset_spec)
    params = _mapping(spec.get("params"))
    prompt = str(spec.get("prompt", "")).strip()
    negative = str(
        params.get("effective_negative_prompt") or spec.get("negative_prompt", "")
    ).strip()
    size = spec.get("size") or params.get("actual_size")
    seed = params.get("seed")
    references = params.get("references")
    refs = [str(one) for one in references] if isinstance(references, (list, tuple)) else []

    lines = [
        f"# {title}",
        "",
        f"- 生成记录：`{generation_id}`",
        f"- 定稿图片：`{final_path}`",
        f"- 尺寸：{_display(size)}",
        f"- Seed：{_display(seed)}",
        "",
        "## 正向提示词",
        "",
        "```text",
        prompt or "（无）",
        "```",
        "",
        "## 负向提示词",
        "",
        "```text",
        negative or "（无）",
        "```",
    ]

    if spec.get("view_layout") or spec.get("view_positions"):
        layout_snapshot = {
            "view_layout": spec.get("view_layout"),
            "view_positions": spec.get("view_positions"),
            "background_color": spec.get("view_background_color"),
        }
        lines.extend(
            [
                "",
                "## 四视图布局",
                "",
                "```json",
                json.dumps(layout_snapshot, ensure_ascii=False, indent=2, default=str),
                "```",
            ]
        )

    lines.extend(["", "## 参考图", ""])
    lines.extend(f"- `{one}`" for one in refs)
    if not refs:
        lines.append("（无）")
    lines.extend(
        [
            "",
            "## 生成参数",
            "",
            "```json",
            json.dumps(params, ensure_ascii=False, indent=2, default=str),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def write_prompt_document(
    ref: ProjectRef,
    *,
    asset_dir: str,
    file_name: str,
    title: str,
    generation_id: str,
    final_path: str,
    asset_spec: Mapping[str, Any],
) -> str:
    """将被采用 generation 的提示词写入素材 `docs/`。"""
    target = layout.asset_document_path(ref.absolute(asset_dir), file_name)
    relative = ref.relative(target)
    return archive.write_text(
        ref,
        target_path=relative,
        content=prompt_markdown(
            title=title,
            generation_id=generation_id,
            final_path=final_path,
            asset_spec=asset_spec,
        ),
    )
