"""四视图编排：单次生成 2048×2048 的 2×2 四宫格，检查后由人选择定稿。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.orm import Session

from atelier.agents import dispatch, parsing
from atelier.assets import archive, characters, generations, imaging, layout, projects
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import Character, Generation
from atelier.db.task_events import record as record_event
from atelier.errors import Conflict
from atelier.providers import image_gen
from atelier.settings import get_settings

_log = structlog.get_logger(__name__)

PAINTER = "image_i2i"
STAGE = generations.VIEWS
POSE_TEMPLATE = "人物姿势模版.jpg"
SHEET_CODE = "sheet"
SHEET_LABEL = "四视图"
VIEW_SIZE = 2048
VIEW_LAYOUT = "grid-2x2"


@dataclass(frozen=True, slots=True)
class Variant:
    code: str
    label: str
    stem: str
    clause: str
    position: str


VARIANTS: tuple[Variant, ...] = (
    Variant(
        code="front",
        label="正面",
        stem="正面",
        clause="front view, facing the camera straight on, symmetrical, full body",
        position="top-left",
    ),
    Variant(
        code="right",
        label="右侧 30°",
        stem="右侧",
        clause=(
            "three-quarter view from the character's right side, about 30 degrees turned, "
            "full body"
        ),
        position="top-right",
    ),
    Variant(
        code="back",
        label="背面",
        stem="背面",
        clause="back view, facing directly away from the camera, full body",
        position="bottom-left",
    ),
    Variant(
        code="left",
        label="左侧 30°",
        stem="左侧",
        clause=(
            "three-quarter view from the character's left side, about 30 degrees turned, "
            "full body"
        ),
        position="bottom-right",
    ),
)
BY_CODE = {one.code: one for one in VARIANTS}
VIEW_POSITIONS = {one.position: one.code for one in VARIANTS}

BACKGROUND_CLAUSE = (
    "Use one perfectly uniform solid {color} background across all four cells, no gradient, "
    "no ground plane, no cast shadow, no environment, even flat lighting"
)
LAYOUT_CLAUSE = (
    "Create exactly one 2048x2048 square image in a strict 2x2 grid: "
    "top-left is front view; top-right is right-side 30-degree view; "
    "bottom-left is back view; bottom-right is left-side 30-degree view. "
    "Exactly one full-body character in each cell, with the same identity, outfit, "
    "proportions, scale and baseline. Keep every character inside its own 1024x1024 cell. "
    "No labels, borders, divider lines or extra panels. The character must not wear a cape, "
    "cloak, mantle, poncho, long robe, long coat, hanging fabric or trailing garment; keep "
    "the torso, arms and legs fully readable for 3D modeling and animation rigging"
)
NEGATIVE_MUST = (
    "environment",
    "scenery",
    "gradient background",
    "textured background",
    "ground plane",
    "cast shadow",
    "transparent background",
    "alpha channel",
    "checkerboard",
    "text",
    "watermark",
    "panel labels",
    "divider lines",
    "missing view",
    "duplicate view",
    "character crossing cells",
    "cropped body",
    "cape",
    "cloak",
    "mantle",
    "poncho",
    "long robe",
    "long coat",
    "loose flowing cloth",
    "hanging fabric",
    "trailing garment",
)
APPENDAGE_CLAUSE = (
    "appendage count must be exact and each one clearly separated in every applicable "
    "view: {items}; distinct, not merged, no overlapping"
)


@dataclass(frozen=True, slots=True)
class ViewImage:
    variant: str
    label: str
    generation_id: str
    file_path: str
    width: int
    height: int
    params: dict[str, Any]
    problems: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ViewFailure:
    variant: str
    label: str
    reason: str


@dataclass(frozen=True, slots=True)
class ViewSet:
    character_id: str
    state: str
    images: tuple[ViewImage, ...]
    failures: tuple[ViewFailure, ...]
    references: tuple[str, ...]
    size_complaint: str | None = None

    @property
    def ok(self) -> bool:
        return (
            not self.failures and bool(self.images) and all(not one.problems for one in self.images)
        )


def pose_template(ref: ProjectRef) -> Path:
    """姿势模版：项目配置 → 项目 templates → 全局 templates。"""
    config = projects.read_config(ref.dir)
    if config.pose_template:
        picked = ref.absolute(config.pose_template)
        if picked.is_file():
            return picked
        raise Conflict(
            f"project.json 里指的姿势模版 {config.pose_template} 不在磁盘上，改回默认或把文件放回去"
        )
    local = ref.dir / layout.TEMPLATES_DIR / POSE_TEMPLATE
    if local.is_file():
        return local
    shared = get_settings().templates_dir / POSE_TEMPLATE
    if shared.is_file():
        return shared
    raise Conflict(
        f"找不到姿势模版 {POSE_TEMPLATE}（项目 templates/ 与全局 templates/ 里都没有）。"
        "四视图必须有它才能保证四个角度是同一套排版"
    )


def reference_images(ref: ProjectRef, character: Character) -> tuple[Path, Path]:
    """参考图顺序固定为姿势模版、定稿渲染图。"""
    characters.require_state(character, characters.RENDER_CONFIRMED, action="生成四视图")
    if not character.render_path:
        raise Conflict(f"{character.name} 还没有定稿渲染图，先过渲染图那道门禁")
    render = ref.absolute(character.render_path)
    if not render.is_file():
        raise Conflict(f"定稿渲染图 {character.render_path} 不在磁盘上了，重新定稿一张再出四视图")
    return pose_template(ref), render


def base_card(project: Session, character: Character) -> dict[str, Any]:
    """四视图沿用渲染图定稿卡片。"""
    row = generations.final(project, target_ref=character.id, stage=generations.RENDER)
    if row is None:
        raise Conflict(f"{character.name} 的渲染图台账里没有定稿那一行，先把渲染图定稿")
    card = dict(row.asset_spec or {})
    if not str(card.get("prompt", "")).strip():
        raise Conflict("渲染图那一版卡片没留下 prompt，四视图没有规格可依，重新出一张渲染图")
    background = parsing.normalize_view_background_color(str(card.get("view_background_color", "")))
    if not background:
        raise Conflict("渲染图定稿卡片缺少有效的四视图背景色，请重新生成并定稿渲染规格卡片")
    card["view_background_color"] = background
    return card


def appendage_clause(character: Character) -> str:
    items = [
        f"{one.get('item', '').strip()} = {one.get('value', '').strip()}"
        for one in characters.hard_constraints(character)
        if one.get("item")
    ]
    return APPENDAGE_CLAUSE.format(items="; ".join(items)) if items else ""


def build_prompt(card: Mapping[str, Any], character: Character) -> str:
    """构造单张四宫格 prompt。"""
    background = parsing.normalize_view_background_color(str(card.get("view_background_color", "")))
    if not background:
        raise Conflict("渲染图卡片缺少有效的四视图背景色")
    layers = [
        str(card.get("prompt", "")).strip(),
        LAYOUT_CLAUSE,
        "; ".join(f"{one.position}: {one.clause}" for one in VARIANTS),
        BACKGROUND_CLAUSE.format(color=background),
        appendage_clause(character),
    ]
    return ", ".join(one for one in layers if one)


def build_negative(card: Mapping[str, Any]) -> str:
    written = str(card.get("negative_prompt", "")).strip()
    lowered = written.lower()
    missing = [one for one in NEGATIVE_MUST if one not in lowered]
    return ", ".join(one for one in [written, *missing] if one)


def image_size(_ref: ProjectRef, _card: Mapping[str, Any]) -> tuple[int, int]:
    return VIEW_SIZE, VIEW_SIZE


def generate_views(
    project: Session,
    runtime: Session,
    ref: ProjectRef,
    character: Character,
    *,
    variants: Sequence[Variant] = (),
    generate: dispatch.ImageFn | None = None,
    seed: int | None = None,
) -> ViewSet:
    """一次生成完整四宫格；不再支持单独重生某个视角。"""
    if variants:
        raise Conflict("四视图现在是一张 2×2 四宫格，只能整张生成或重生")
    template, render = reference_images(ref, character)
    card = base_card(project, character)
    background_color = str(card["view_background_color"])
    references = (template, render)
    labels = (_ref_label(ref, template), character.render_path or "")

    try:
        reply = dispatch.draw(
            runtime,
            PAINTER,
            build_prompt(card, character),
            generate or image_gen.generate,
            negative_prompt=build_negative(card),
            width=VIEW_SIZE,
            height=VIEW_SIZE,
            seed=seed,
            references=list(references),
            project_code=ref.code,
            task_id=character.id,
        )
    except Exception as exc:  # noqa: BLE001 - 保留失败原因供前端展示
        reason = str(exc)
        record_event(
            project,
            character.id,
            "view_failed",
            f"四视图没出来：{reason}",
            {"variant": SHEET_CODE, "reason": reason},
            level="error",
        )
        project.commit()
        return ViewSet(
            character_id=character.id,
            state=character.state,
            images=(),
            failures=(ViewFailure(SHEET_CODE, SHEET_LABEL, reason),),
            references=labels,
        )

    image, report = _land(
        project,
        ref,
        character,
        reply,
        card=card,
        background_color=background_color,
        references=references,
    )
    if report.ok and not characters.at_least(character, characters.VIEWS_GENERATED):
        character.state = characters.VIEWS_GENERATED
        record_event(
            project,
            character.id,
            "state_advanced",
            f"{character.name} 进入「{characters.label(characters.VIEWS_GENERATED)}」",
            {"state": characters.VIEWS_GENERATED},
        )
    project.commit()

    result = ViewSet(
        character_id=character.id,
        state=character.state,
        images=(image,),
        failures=(),
        references=labels,
    )
    _write_meta(ref, character, result, card=card)
    _log.info("views_generated", id=character.id, state=character.state, ok=result.ok)
    return result


def _ref_label(ref: ProjectRef, path: Path) -> str:
    return ref.relative(path) if path.is_relative_to(ref.dir) else str(path)


def _land(
    project: Session,
    ref: ProjectRef,
    character: Character,
    reply: image_gen.ImageReply,
    *,
    card: Mapping[str, Any],
    background_color: str,
    references: Sequence[Path],
) -> tuple[ViewImage, imaging.Report]:
    report = imaging.measure_grid(reply.data, background_color=background_color)
    relative = archive.stage_bytes(
        ref,
        asset_dir=character.dir_name,
        stem=f"{character.name}_四视图",
        suffix=reply.suffix,
        data=reply.data,
    )
    params = {
        **reply.params,
        "references": [_ref_label(ref, one) for one in references],
        "variant": SHEET_CODE,
        "view_layout": VIEW_LAYOUT,
        "view_positions": VIEW_POSITIONS,
        "background": {
            "target_color": report.target_color,
            "edge_match": report.edge_match,
            "transparent": report.transparent,
            "subject": report.subject,
            "regions": [
                {
                    "code": one.code,
                    "label": one.label,
                    "edge_match": one.edge_match,
                    "transparent": one.transparent,
                    "subject": one.subject,
                    "problems": list(one.problems),
                }
                for one in report.regions
            ],
        },
    }
    snapshot = {
        **card,
        "variant": SHEET_CODE,
        "view_layout": VIEW_LAYOUT,
        "view_positions": VIEW_POSITIONS,
        "params": params,
    }
    row = generations.record(
        project,
        target_ref=character.id,
        stage=STAGE,
        variant=SHEET_CODE,
        file_path=relative,
        file_hash=archive.file_hash(ref.absolute(relative)),
        task_id=character.id,
        asset_spec=snapshot,
    )
    record_event(
        project,
        character.id,
        "view_generated" if report.ok else "view_suspect",
        f"四视图：{relative}（{report.size}）"
        + ("" if report.ok else "\n" + "\n".join(f"- {one}" for one in report.problems)),
        {
            "variant": SHEET_CODE,
            "generation_id": row.id,
            "file_path": relative,
            "problems": list(report.problems),
            "params": params,
        },
        level="info" if report.ok else "warning",
    )
    return (
        ViewImage(
            variant=SHEET_CODE,
            label=SHEET_LABEL,
            generation_id=row.id,
            file_path=relative,
            width=report.width,
            height=report.height,
            params=params,
            problems=report.problems,
        ),
        report,
    )


def _write_meta(
    ref: ProjectRef, character: Character, result: ViewSet, *, card: Mapping[str, Any]
) -> None:
    archive.merge_meta(
        characters.meta_path(ref, character),
        {
            "views": {
                "state": result.state,
                "view_layout": VIEW_LAYOUT,
                "view_positions": VIEW_POSITIONS,
                "asset_spec": dict(card),
                "references": list(result.references),
                "generated_at": datetime.now(UTC).isoformat(),
                "images": [
                    {
                        "variant": one.variant,
                        "generation_id": one.generation_id,
                        "file_path": one.file_path,
                        "size": f"{one.width}x{one.height}",
                        "problems": list(one.problems),
                        "params": one.params,
                    }
                    for one in result.images
                ],
                "failures": [
                    {"variant": one.variant, "reason": one.reason} for one in result.failures
                ],
            }
        },
    )
    characters.sync_meta(ref, character)


def latest_by_variant(project: Session, character: Character) -> dict[str, Generation]:
    """兼容读取新 sheet 与旧 front/right/back/left 候选。"""
    picked: dict[str, Generation] = {}
    for row in generations.candidates(project, target_ref=character.id, stage=STAGE):
        if row.variant and row.variant not in picked:
            picked[row.variant] = row
    return picked


def latest_sheet(project: Session, character: Character) -> Generation | None:
    return latest_by_variant(project, character).get(SHEET_CODE)


def adopt(
    project: Session,
    ref: ProjectRef,
    character: Character,
    chosen: Mapping[str, Generation],
    *,
    note: str = "",
) -> dict[str, archive.ArchiveResult]:
    """把选定的一张四宫格拷到定稿位；旧四张分图不能混入新定稿。"""
    if set(chosen) != {SHEET_CODE}:
        raise Conflict("新四视图必须选择一张完整四宫格定稿；旧分图只能查看，不能混合定稿")
    row = chosen[SHEET_CODE]
    if row.target_ref != character.id or row.stage != STAGE or row.variant != SHEET_CODE:
        raise Conflict("这条产物不是该角色的四视图四宫格")
    source = ref.absolute(row.file_path)
    if not source.is_file():
        raise Conflict(f"四视图 {row.file_path} 不在磁盘上了，重新生成一张再定稿")
    background = str((row.asset_spec or {}).get("view_background_color", ""))
    report = imaging.measure_grid(source.read_bytes(), background_color=background)
    if not report.ok:
        raise Conflict(f"四视图机器校验未通过：{report.problems[0]}")

    suffix = Path(row.file_path).suffix or ".png"
    result = archive.adopt_file(
        ref,
        source_path=row.file_path,
        target_path=characters.views_target(character, "四视图", suffix),
        extra={
            "stage": STAGE,
            "variant": SHEET_CODE,
            "view_layout": VIEW_LAYOUT,
            "generation_id": row.id,
            "note": note,
        },
    )
    generations.mark_final(
        project, row, file_path=result.target_path, file_hash=result.content_hash
    )
    characters.confirm_views(
        project,
        ref,
        character,
        paths={SHEET_CODE: result.target_path},
        note=note,
    )
    _log.info("views_adopted", id=character.id, views=1)
    return {SHEET_CODE: result}
