"""四视图编排：`image_i2i` 并发生成统一纯色背景视图 → 机器检查 → 人选输入定稿。

跟渲染图那一步最大的差别是**这一步没有想象空间**。渲染图是从设定文字长出一张图，四视图是
把已经定稿的那一张翻到另外三个面去看，所以：

1. **两张参考图缺一不做**。姿势模版给视角、站姿、排版与白底规范，定稿渲染图给外观、配色与
   材质。少了模版，四张图的角度与画面比例各说各话；少了渲染图，模型就会照着设定文字重新想
   象一个角色出来——那不是「同一个角色的另一个面」。
2. **prompt 不重新找 `prompt_smith` 要**。这四张的规格就是渲染图那张卡片，改动卡片等于让四
   视图与已经定稿的渲染图不是同一个角色。平台只在卡片外面追加视角句与白底约束。
3. **白底靠 negative 硬压，不靠删层**。卡片里的环境层是模型写的自由文本，程序没法可靠地把
   它切出来；能可靠做到的是把白底写进正向、把 `background`/`environment` 这些词钉进
   negative，让后者压住前者。
4. **背面图强制注入附属结构的数量与分离状态**。背面是尾巴、翅膀、披风最容易糊成一团的角
   度，而糊在一起要到建模出网格时才看得出来，那时候重来的代价是整条流水线。

四个变体并发发出去：一张 4K 图三五十秒，串行等于让用户干等四倍时间。并发只并发**发请求**
这一段——每个线程开自己的 runtime Session 记路由与额度的账（Session 不是线程安全的），落盘、
登台账、写事件全回到主线程按变体顺序做，台账里的次序才是稳定的。

某个变体失败不拖累其他三个：把失败原因记成事件、其余照样落盘，用户重生那一个变体就行。四张
里少一张就不推 S4——S4 的含义是「四个面都有图可看了」。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.engine import Connection, Engine
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
"""姿势模版的文件名。解析顺序是 项目 `project.json` 指定 → 项目 `templates/` → 全局
`templates/`：项目可以有自己的排版规范，没有就用仓库里那份通用的。"""


@dataclass(frozen=True, slots=True)
class Variant:
    """一个视角：台账用 `code`，文件名用 `stem`，说给人听用 `label`。

    台账与接口路径用 ASCII 的 `code`，文件名用中文的 `stem`——落到磁盘上的名字是给人翻目录
    看的，`character_赤瞳_背面.png` 比 `character_赤瞳_back.png` 认得快。
    """

    code: str
    label: str
    stem: str
    clause: str
    """追加到 prompt 末尾的视角句。用英文写：生图模型对视角词的英文识别率明显更高。"""

    appendages: bool = False
    """要不要把附属结构的数量与分离状态强制注入。只有背面图要。"""


VARIANTS: tuple[Variant, ...] = (
    Variant(
        code="front",
        label="正面",
        stem="正面",
        clause="front view, facing the camera straight on, symmetrical, full body",
    ),
    Variant(
        code="right",
        label="右侧 30°",
        stem="右侧",
        clause=(
            "three-quarter view from the character's right side, about 30 degrees turned, full body"
        ),
    ),
    Variant(
        code="back",
        label="背面",
        stem="背面",
        clause="back view, facing directly away from the camera, full body",
        appendages=True,
    ),
    Variant(
        code="left",
        label="左侧 30°",
        stem="左侧",
        clause=(
            "three-quarter view from the character's left side, about 30 degrees turned, full body"
        ),
    ),
)

BY_CODE = {one.code: one for one in VARIANTS}

BACKGROUND_CLAUSE = (
    "Solid {color} background, perfectly uniform, no gradient, no ground plane, no cast shadow, "
    "no environment, even flat lighting, character centered in frame"
)
"""四视图动态纯色背景约束，四个视角共用渲染卡片里选定的颜色。"""

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
)
"""negative 里必须有的词。卡片自己写了就不重复塞，缺的补上——这几项是四视图能不能进建模
的前提，不能指望卡片每次都写全。"""

APPENDAGE_CLAUSE = (
    "appendage count must be exact and each one clearly separated: {items}; "
    "distinct, side by side, not merged, no overlapping"
)


@dataclass(frozen=True, slots=True)
class ViewImage:
    """一个视角出来的一张候选。"""

    variant: str
    label: str
    generation_id: str
    file_path: str
    """`tmp/` 下的相对路径。定稿位要等人选完输入才有。"""

    width: int
    height: int
    params: dict[str, Any]
    problems: tuple[str, ...]
    """机器量出来的问题（背景不纯、尺寸不对）。不为空也照样留着图：判定归 `vision_reviewer`
    与人工，这里只把事实摆出来。"""


@dataclass(frozen=True, slots=True)
class ViewFailure:
    variant: str
    label: str
    reason: str


@dataclass(frozen=True, slots=True)
class ViewSet:
    """一批四视图的结果。"""

    character_id: str
    state: str
    images: tuple[ViewImage, ...]
    failures: tuple[ViewFailure, ...]
    references: tuple[str, ...]
    """两张参考图的相对路径（模版可能在仓库全局，那就是绝对路径）。"""

    size_complaint: str | None = None
    """四张尺寸不齐时的说明。齐了是 None。"""

    @property
    def ok(self) -> bool:
        return not self.failures and all(not one.problems for one in self.images)


# --------------------------------------------------------------------------- #
# 参考图
# --------------------------------------------------------------------------- #


def pose_template(ref: ProjectRef) -> Path:
    """姿势模版：`project.json` 指定 → 项目 `templates/` → 全局 `templates/`。

    一层都找不到就拒绝执行，不退化成纯文字生成：纯文字出的四张图角度与画幅各不相同，拼在
    一起看就是四个角色，而这件事在图生成完之前用户是看不出来的。
    """
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
    """两张参考图，缺一即拒。顺序固定：先模版后渲染图。

    顺序也是约定的一部分：模版排在前面表示「按这个视角与排版画」，渲染图排在后面表示「画的是
    这个角色」。两家的 i2i 都按顺序理解参考图的主次，倒过来传会让模版的画风盖过角色。
    """
    characters.require_state(character, characters.RENDER_CONFIRMED, action="生成四视图")
    if not character.render_path:
        raise Conflict(f"{character.name} 还没有定稿渲染图，先过渲染图那道门禁")
    render = ref.absolute(character.render_path)
    if not render.is_file():
        raise Conflict(f"定稿渲染图 {character.render_path} 不在磁盘上了，重新定稿一张再出四视图")
    return pose_template(ref), render


def base_card(project: Session, character: Character) -> dict[str, Any]:
    """四视图共用的卡片：就是渲染图定稿那一版的卡片。

    不另出一版：卡片是「这个角色长什么样」的规格，渲染图已经按它定过稿了，四视图再要一版
    等于允许两版规格并存，出来的图跟定稿渲染图对不上时谁也说不清该改哪一版。
    """
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


# --------------------------------------------------------------------------- #
# prompt 组装
# --------------------------------------------------------------------------- #


def appendage_clause(character: Character) -> str:
    """把硬性约束清单翻成一句「数量准确且互相分开」。

    照抄清单原文而不是自己改写：清单是 `spec_reviewer` 对设定的翻译，改写一遍就多了一层可能
    出错的转述，而评审回头还要拿同一份清单逐条比对。
    """
    items = [
        f"{one.get('item', '').strip()} = {one.get('value', '').strip()}"
        for one in characters.hard_constraints(character)
        if one.get("item")
    ]
    if not items:
        return ""
    return APPENDAGE_CLAUSE.format(items="; ".join(items))


def build_prompt(card: Mapping[str, Any], variant: Variant, character: Character) -> str:
    """卡片 prompt + 视角句 + 动态纯色背景句（背面再加附属结构句）。"""
    background = parsing.normalize_view_background_color(str(card.get("view_background_color", "")))
    if not background:
        raise Conflict("渲染图卡片缺少有效的四视图背景色")
    layers = [
        str(card.get("prompt", "")).strip(),
        variant.clause,
        BACKGROUND_CLAUSE.format(color=background),
    ]
    if variant.appendages:
        layers.append(appendage_clause(character))
    return ", ".join(one for one in layers if one)


def build_negative(card: Mapping[str, Any]) -> str:
    """卡片 negative 加上纯色背景必备禁止项，已经写了的不重复塞。"""
    written = str(card.get("negative_prompt", "")).strip()
    lowered = written.lower()
    missing = [one for one in NEGATIVE_MUST if one not in lowered]
    return ", ".join(one for one in [written, *missing] if one)


def image_size(ref: ProjectRef, card: Mapping[str, Any]) -> tuple[int, int]:
    """尺寸跟渲染图那张卡片一致，卡片没写就取项目 `defaults.image_size`。

    四张与渲染图同规格是硬要求：建模吃的是这一组图，其中一张画幅不同就相当于告诉它这个面的
    比例不一样。
    """
    size = str(card.get("size", ""))
    width, _, height = size.partition("x")
    if width.isdigit() and height.isdigit() and int(width) > 0 and int(height) > 0:
        return int(width), int(height)
    edge = projects.read_config(ref.dir).defaults.image_size or image_gen.DEFAULT_SIZE
    return edge, edge


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #


def _draw(
    bind: Engine | Connection,
    *,
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    seed: int | None,
    references: Sequence[Path],
    generate: dispatch.ImageFn,
    project_code: str,
    task_id: str,
) -> image_gen.ImageReply:
    """在自己的 runtime Session 上发一次生图请求。

    每个线程一个 Session：路由层要记额度与熔断，而 Session 不是线程安全的，四个线程共用一个
    迟早会在并发写的时候把账记乱。engine 是共享的，连接池自己会分连接。
    """
    with Session(bind) as session:
        return dispatch.draw(
            session,
            PAINTER,
            prompt,
            generate,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            seed=seed,
            references=list(references),
            project_code=project_code,
            task_id=task_id,
        )


def generate_views(
    project: Session,
    runtime: Session,
    ref: ProjectRef,
    character: Character,
    *,
    variants: Sequence[Variant] = VARIANTS,
    generate: dispatch.ImageFn | None = None,
    seed: int | None = None,
) -> ViewSet:
    """出一批四视图：并发生图、逐张量白底、落 `tmp/`、登台账，四个面都齐了推到 S4。

    `variants` 可以只给几个，用于重生某一个角度——评审驳回时往往只有背面不合格，四张全重来
    既费额度又会把已经认可的三张换掉。
    """
    template, render = reference_images(ref, character)
    card = base_card(project, character)
    background_color = str(card["view_background_color"])
    width, height = image_size(ref, card)
    negative = build_negative(card)
    references = (template, render)
    bind = runtime.get_bind()

    picked = tuple(variants)
    if not picked:
        raise Conflict("没有指定要生成哪个视角")

    replies: dict[str, image_gen.ImageReply] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(picked)) as pool:
        futures = {
            pool.submit(
                _draw,
                bind,
                prompt=build_prompt(card, one, character),
                negative_prompt=negative,
                width=width,
                height=height,
                seed=seed,
                references=references,
                generate=generate or image_gen.generate,
                project_code=ref.code,
                task_id=character.id,
            ): one
            for one in picked
        }
        for future, variant in futures.items():
            try:
                replies[variant.code] = future.result()
            except Exception as exc:  # noqa: BLE001 - 一个视角失败不该带走另外三个
                errors[variant.code] = str(exc)

    images: list[ViewImage] = []
    reports: list[imaging.Report] = []
    failures: list[ViewFailure] = []
    for variant in picked:
        reply = replies.get(variant.code)
        if reply is None:
            reason = errors.get(variant.code, "没有拿回图")
            failures.append(ViewFailure(variant=variant.code, label=variant.label, reason=reason))
            record_event(
                project,
                character.id,
                "view_failed",
                f"{variant.label}没出来：{reason}",
                {"variant": variant.code, "reason": reason},
                level="error",
            )
            continue
        image, report = _land(
            project,
            ref,
            character,
            variant,
            reply,
            card=card,
            background_color=background_color,
            expect=(width, height),
            references=references,
        )
        images.append(image)
        reports.append(report)

    state = _maybe_advance(project, character)
    project.commit()

    result = ViewSet(
        character_id=character.id,
        state=state,
        images=tuple(images),
        failures=tuple(failures),
        references=(_ref_label(ref, template), character.render_path or ""),
        size_complaint=imaging.same_size(reports),
    )
    _write_meta(ref, character, result, card=card)
    _log.info(
        "views_generated",
        id=character.id,
        state=state,
        made=len(images),
        failed=len(failures),
    )
    return result


def _ref_label(ref: ProjectRef, path: Path) -> str:
    """参考图记进台账时的写法：在项目里就记相对路径，在仓库全局模版里就记给它的原样。

    项目内的一律记相对：项目目录整体搬到另一台机器上时，绝对路径全都作废。
    """
    return ref.relative(path) if path.is_relative_to(ref.dir) else str(path)


def _land(
    project: Session,
    ref: ProjectRef,
    character: Character,
    variant: Variant,
    reply: image_gen.ImageReply,
    *,
    card: Mapping[str, Any],
    background_color: str,
    expect: tuple[int, int],
    references: Sequence[Path],
) -> tuple[ViewImage, imaging.Report]:
    """一张图落 `tmp/`、量一遍、登台账、写事件。都在主线程做，次序才稳定。"""
    report = imaging.measure(reply.data, expect=expect, background_color=background_color)
    relative = archive.stage_bytes(
        ref,
        asset_dir=character.dir_name,
        stem=f"{character.name}_{variant.stem}",
        suffix=reply.suffix,
        data=reply.data,
    )
    params = {
        **reply.params,
        "references": [_ref_label(ref, one) for one in references],
        "variant": variant.code,
        "background": {
            "target_color": report.target_color,
            "edge_match": report.edge_match,
            "transparent": report.transparent,
            "subject": report.subject,
        },
    }
    row = generations.record(
        project,
        target_ref=character.id,
        stage=STAGE,
        variant=variant.code,
        file_path=relative,
        file_hash=archive.file_hash(ref.absolute(relative)),
        task_id=character.id,
        asset_spec={**card, "variant": variant.code, "params": params},
    )
    record_event(
        project,
        character.id,
        "view_generated" if report.ok else "view_suspect",
        f"{variant.label}：{relative}（{report.size}）"
        + ("" if report.ok else "\n" + "\n".join(f"- {one}" for one in report.problems)),
        {
            "variant": variant.code,
            "generation_id": row.id,
            "file_path": relative,
            "problems": list(report.problems),
            "params": params,
        },
        level="info" if report.ok else "warning",
    )
    return (
        ViewImage(
            variant=variant.code,
            label=variant.label,
            generation_id=row.id,
            file_path=relative,
            width=report.width,
            height=report.height,
            params=params,
            problems=report.problems,
        ),
        report,
    )


def _maybe_advance(project: Session, character: Character) -> str:
    """四个面都有候选了才推 S4。

    少一张就不推：S4 的含义是「四个面都有图可看」，下一步的评审与定稿都按这个含义办事，
    提前推等于让门禁按钮凭一个不成立的前提亮起来。
    """
    if characters.at_least(character, characters.VIEWS_GENERATED):
        return character.state
    covered = {
        row.variant
        for row in generations.candidates(project, target_ref=character.id, stage=STAGE)
        if row.variant
    }
    if not all(one.code in covered for one in VARIANTS):
        return character.state
    character.state = characters.VIEWS_GENERATED
    record_event(
        project,
        character.id,
        "state_advanced",
        f"{character.name} 进入「{characters.label(characters.VIEWS_GENERATED)}」",
        {"state": characters.VIEWS_GENERATED},
    )
    return character.state


def _write_meta(
    ref: ProjectRef, character: Character, result: ViewSet, *, card: Mapping[str, Any]
) -> None:
    """参数快照进 `meta.json`：模型、两张参考图、尺寸、seed 都在里面，半年后能复现。"""
    archive.merge_meta(
        characters.meta_path(ref, character),
        {
            "views": {
                "state": result.state,
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


# --------------------------------------------------------------------------- #
# 定稿归档
# --------------------------------------------------------------------------- #


def latest_by_variant(project: Session, character: Character) -> dict[str, Generation]:
    """每个视角最近的一张候选。人不指名时按它来。"""
    picked: dict[str, Generation] = {}
    for row in generations.candidates(project, target_ref=character.id, stage=STAGE):
        if row.variant and row.variant not in picked:
            picked[row.variant] = row
    return picked


def adopt(
    project: Session,
    ref: ProjectRef,
    character: Character,
    chosen: Mapping[str, Generation],
    *,
    note: str = "",
) -> dict[str, archive.ArchiveResult]:
    """把选定的四张拷到定稿位，台账逐个标定稿，状态推到 S5。

    四个视角一次性定：建模吃的是一整组图，只定两张就定稿等于允许一组里两张新两张旧，而新旧
    混用出来的模型是错的却看不出为什么。
    """
    missing = [one.label for one in VARIANTS if one.code not in chosen]
    if missing:
        raise Conflict(f"四视图还差 {'、'.join(missing)}，凑齐四个角度再定稿")

    results: dict[str, archive.ArchiveResult] = {}
    for variant in VARIANTS:
        row = chosen[variant.code]
        if row.target_ref != character.id or row.stage != STAGE or row.variant != variant.code:
            raise Conflict(f"{variant.label}那一条产物不是该角色的这个视角")
        suffix = Path(row.file_path).suffix or ".png"
        result = archive.adopt_file(
            ref,
            source_path=row.file_path,
            target_path=characters.views_target(character, variant.stem, suffix),
            extra={"stage": STAGE, "variant": variant.code, "generation_id": row.id, "note": note},
        )
        generations.mark_final(
            project, row, file_path=result.target_path, file_hash=result.content_hash
        )
        results[variant.code] = result

    characters.confirm_views(
        project,
        ref,
        character,
        paths={code: one.target_path for code, one in results.items()},
        note=note,
    )
    _log.info("views_adopted", id=character.id, views=len(results))
    return results
