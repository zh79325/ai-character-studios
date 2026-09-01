"""渲染图这一步的编排：`prompt_smith` 出卡片 → `image_t2i` 出图 → 人工门禁定稿。

这一步只做三件事，且顺序不可换：

1. **先要卡片再生图**。卡片把设定翻译成有层序的 prompt 与可数的硬性约束，是这张图唯一的
   规格。跳过它直接把设定原文丢给生图模型，出来的图没法说清哪里不符合要求——没有规格就没有
   不合格。
2. **图落 `tmp/`，不进定稿位**。一次出图几十秒，用户要在几张之间挑；生成即定稿等于 Agent
   替人拍了板。
3. **采用是人的动作**。`adopt` 由门禁调用，此前 `state` 一步都不动。

卡片有缺项时（缺尺寸、缺 prompt、文件名不合法）不硬着头皮生图：拿一份缺层的 prompt 出图，
花掉的额度和时间换回来一张必然要重做的图，还要用户自己看出问题在卡片上。缺项写进事件让人
看见，重跑一次卡片比重跑一次生图便宜得多。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from atelier.agents import audit, context, dispatch, parsing
from atelier.agents import conversation as conv
from atelier.agents.definitions import get_agent
from atelier.agents.parsing import AssetSpec
from atelier.assets import archive, characters, documents, generations, layout, projects
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import Character, Generation
from atelier.db.task_events import record as record_event
from atelier.errors import Conflict
from atelier.providers import image_gen, text_chat

_log = structlog.get_logger(__name__)

SMITH = "prompt_smith"
PAINTER = "image_t2i"

STAGE = generations.RENDER

CHARACTER_IMAGE_SIZE = 2048
"""角色效果图与四视图画布的固定边长。"""

NO_CAPE_NEGATIVE = (
    "cape, cloak, mantle, poncho, long robe, long coat, loose flowing cloth, "
    "hanging fabric, trailing garment"
)
"""建模与动作绑定要求四肢和躯干轮廓无遮挡。"""

_FORBIDDEN_GARMENT_TERMS = (
    "cape",
    "cloak",
    "mantle",
    "poncho",
    "long robe",
    "long coat",
    "披风",
    "斗篷",
    "披肩",
    "长袍",
    "长外套",
)

SPEC_REQUEST = """## 项目视觉规范（art-bible.md）

{art_bible}

## 项目风格基调（project.json 的 style）

{style}

---

请为角色「{name}」出**渲染图（S2，文生图）**的素材规格卡片，只出这一张，按你的输出格式：

- 项目缩写用 `{code}`，编号从 001 起。
- 尺寸固定写 `2048x2048`，格式 png。
- 环境策略按渲染图那一档：可以带环境背景、氛围光与镜头感，不要求透明背景。
- 必须选择四视图统一使用的不透明纯色背景，填写 `四视图背景色：#RRGGBB（颜色名）`。
- 底色应避开角色主色、发光色和半透明部件颜色，优先最大化色相与明度反差。
- prompt 层序不得缺层，附属结构层写明数量与分离状态。
- 人物不得设计或绘制披风、斗篷、披肩、长袍、长外套、垂布或飘带。
- 躯干和四肢轮廓必须清楚，便于建模与动作绑定。
- negative_prompt 必须包含 cape、cloak、long robe、long coat、loose flowing cloth 等禁止词。
"""

RETRY_REQUEST = """这张卡片还不能用，问题如下：

{gaps}

请只改这几处，其余保持原样，改完仍按原格式整张输出。
"""

FORMAT_RETRY_REQUEST = """上一版没有按模板输出可解析的素材规格卡片，原文如下：

{previous}

不要解释、讨论或征求确认。请严格按系统约定的完整卡片模板重新输出，字段名与顺序不得改动。
"""

FIELD_REQUEST = """上一版渲染图在「{field}」这一项上不合要求：

{note}

请只改卡片里跟这一项相关的内容（其余字段与 prompt 的其他层保持原样），整张重新输出。
"""

DIRECTION_REQUEST = """这个方向不对，需要换一版：

{note}

请重新构思视觉描述与 prompt，仍然锚定 art bible 与角色设定，不得改动设定本身。
"""

MAX_SPEC_RETRIES = 2
"""卡片有缺项时最多让 `prompt_smith` 自己补几次。"""

_TRANSPARENT_TERMS = (
    "transparent background",
    "transparent backdrop",
    "alpha channel",
    "透明背景",
    "透明底",
    "透明通道",
    "棋盘格背景",
)


def _spec_gaps(spec: AssetSpec) -> tuple[str, ...]:
    """角色渲染卡片必须使用固定尺寸，且不得带入旧项目的透明要求。"""
    gaps = list(spec.gaps())
    if (spec.width, spec.height) != (CHARACTER_IMAGE_SIZE, CHARACTER_IMAGE_SIZE):
        gaps.append(f"尺寸必须是 {CHARACTER_IMAGE_SIZE}x{CHARACTER_IMAGE_SIZE}")
    prompt = spec.prompt.lower()
    if any(term in prompt for term in _TRANSPARENT_TERMS):
        gaps.append("prompt 不得要求透明背景或 alpha channel")
    if any(term in prompt for term in _FORBIDDEN_GARMENT_TERMS):
        gaps.append("prompt 不得要求披风、斗篷、长袍、长外套等遮挡身体轮廓的服装")
    return tuple(gaps)


def _negative_prompt(spec: AssetSpec) -> str:
    """即使旧卡片没有写禁用服装，也在实际请求中补齐建模硬约束。"""
    written = spec.negative_prompt.strip()
    lowered = written.lower()
    missing = [term for term in NO_CAPE_NEGATIVE.split(", ") if term not in lowered]
    return ", ".join([written, *missing])


REPORT = """IMAGE-RESULT: {status}
文件：{path}
尺寸：{size}
参数快照：{params}
"""
"""`image_t2i` 提示词里约定的回报格式。它是执行者不是对话模型，这段由平台代码填。"""


@dataclass(frozen=True, slots=True)
class RenderResult:
    """一次生图的结果。前端据此显示图与参数，门禁据此知道要采用哪一行。"""

    character_id: str
    generation_id: str
    file_path: str
    """`tmp/` 下的相对路径。定稿位要等人采用之后才有。"""
    spec: AssetSpec
    params: dict[str, Any]
    width: int
    height: int


# --------------------------------------------------------------------------- #
# 卡片
# --------------------------------------------------------------------------- #


def image_size(_ref: ProjectRef, _spec: AssetSpec | None = None) -> tuple[int, int]:
    """角色效果图固定为 2048×2048，不受项目默认值或旧 art bible 覆盖。"""
    return CHARACTER_IMAGE_SIZE, CHARACTER_IMAGE_SIZE


def _style_text(ref: ProjectRef) -> str:
    style = projects.read_config(ref.dir).style
    lines = [
        f"- {label}：{value}"
        for label, value in (
            ("艺术风格", style.art_style),
            ("氛围", style.mood),
            ("色板", style.palette),
            ("画质", style.quality),
        )
        if value
    ]
    return "\n".join(lines) or "（这个项目还没写风格基调，以 art bible 为准）"


def _spec_payload(
    project: Session, ref: ProjectRef, character: Character, request: str
) -> list[dict[str, str]]:
    agent = get_agent(SMITH)
    relative = characters.spec_target(character)
    spec_path = ref.absolute(relative)
    if not spec_path.is_file():
        raise Conflict(f"设定文档 {relative} 不在磁盘上了，先把设定沉淀一份再出卡片")
    assembled = context.assemble(
        agent,
        [context.Ask(content=request)],
        addendum=conv.addendum(ref, SMITH),
        artifact_path=relative,
        artifact_text=spec_path.read_text(encoding="utf-8"),
        project_memories=conv.enabled_memories(project, ref, character.id),
    )
    return assembled.payload()


def make_spec(
    project: Session,
    runtime: Session,
    ref: ProjectRef,
    character: Character,
    *,
    chat: dispatch.ChatFn | None = None,
    note: str = "",
    field: str = "",
    turn_audit: audit.TurnAudit | None = None,
) -> AssetSpec:
    """让 `prompt_smith` 出一张渲染图卡片，缺项就让它自己补，至多 `MAX_SPEC_RETRIES` 次。

    `field` 给了就是「改某一项重生」：只把那一项发回去，别的字段与 prompt 的其他层原样留着
    ——重生一整张会顺手把用户上一轮认可的部分也改掉，他会觉得平台在跟他对着干。
    """
    characters.require_state(character, characters.SPEC_CONFIRMED, action="出渲染图卡片")
    if field:
        request = FIELD_REQUEST.format(field=field, note=note or "（没写具体要求）")
    elif note:
        request = DIRECTION_REQUEST.format(note=note)
    else:
        request = SPEC_REQUEST.format(
            art_bible=projects.read_art_bible(ref).strip() or "（这个项目还没写视觉规范）",
            style=_style_text(ref),
            name=character.name,
            code=ref.code,
        )

    agent = get_agent(SMITH)
    caller = chat or text_chat.complete
    attempt = 1
    while True:
        payload = _spec_payload(project, ref, character, request)

        def audited_chat(
            candidate: Any, messages: Any, attempt_no: int = attempt, **kwargs: Any
        ) -> text_chat.ChatReply:
            max_tokens = text_chat.output_budget(candidate, agent.max_output_tokens)
            if turn_audit is not None:
                turn_audit.write_request(
                    f"生成效果图卡片（第 {attempt_no} 次）",
                    candidate,
                    messages,
                    max_tokens=max_tokens,
                )
            try:
                reply = caller(candidate, messages, **{**kwargs, "max_tokens": max_tokens})
            except Exception as exc:
                if turn_audit is not None:
                    turn_audit.write_error(exc)
                raise
            if turn_audit is not None:
                turn_audit.write_response(reply)
            return reply

        reply = dispatch.run(
            runtime,
            SMITH,
            payload,
            audited_chat,
            project_code=ref.code,
            task_id=character.id,
        )
        cards = parsing.parse_asset_specs(reply.content)
        if not cards:
            record_event(
                project,
                character.id,
                "asset_spec_unparseable",
                reply.content.strip(),
                {"attempt": attempt},
                level="warning",
            )
            project.commit()
            if attempt > MAX_SPEC_RETRIES:
                raise Conflict(f"prompt_smith 连续 {attempt} 次没输出可解析的卡片，请检查审计记录")
            request = FORMAT_RETRY_REQUEST.format(previous=reply.content.strip()[:6000])
            attempt += 1
            continue

        spec = cards[0]
        gaps = _spec_gaps(spec)
        record_event(
            project,
            character.id,
            "asset_spec_drafted" if not gaps else "asset_spec_incomplete",
            spec.text or reply.content.strip(),
            {"attempt": attempt, "spec": spec.as_dict(), "gaps": list(gaps)},
            level="info" if not gaps else "warning",
        )
        project.commit()
        if not gaps or attempt > MAX_SPEC_RETRIES:
            if gaps:
                raise Conflict(
                    f"卡片还缺 {'、'.join(gaps)}，补了 {attempt - 1} 次仍不齐，"
                    "先看看设定与 art bible 是不是缺了对应的信息"
                )
            _log.info("asset_spec_drafted", id=character.id, code=spec.code, attempt=attempt)
            return spec
        request = RETRY_REQUEST.format(gaps="\n".join(f"- {one}" for one in gaps))
        attempt += 1


# --------------------------------------------------------------------------- #
# 生图
# --------------------------------------------------------------------------- #


def _report(*, path: str, size: str, params: dict[str, Any]) -> str:
    listed = " / ".join(
        str(params.get(key))
        for key in ("model", "actual_size", "latency_ms")
        if params.get(key) is not None
    )
    return REPORT.format(status="OK", path=path, size=size, params=listed or "（无）")


def render(
    project: Session,
    runtime: Session,
    ref: ProjectRef,
    character: Character,
    *,
    spec: AssetSpec | None = None,
    chat: dispatch.ChatFn | None = None,
    generate: dispatch.ImageFn | None = None,
    note: str = "",
    field: str = "",
    turn_audit: audit.TurnAudit | None = None,
) -> RenderResult:
    """出一张渲染图：拿卡片、生图、落 `tmp/`、登台账，状态推到 S2。

    推到 S2 而不是等采用：S2 的意思是「有图可看了」，门禁按钮要凭它才出现。采用与否是 S3 的
    事，两件事分开记，事后才看得出「生成过几版、最后采用了哪一版」。
    """
    characters.require_state(character, characters.SPEC_CONFIRMED, action="生成渲染图")
    card = spec or make_spec(
        project,
        runtime,
        ref,
        character,
        chat=chat,
        note=note,
        field=field,
        turn_audit=turn_audit,
    )
    gaps = _spec_gaps(card)
    if gaps:
        raise Conflict(f"渲染图卡片还不能使用：{'、'.join(gaps)}")
    width, height = image_size(ref, card)
    negative_prompt = _negative_prompt(card)

    reply = dispatch.draw(
        runtime,
        PAINTER,
        card.prompt,
        generate or image_gen.generate,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        project_code=ref.code,
        task_id=character.id,
    )

    if (reply.width, reply.height) != (width, height):
        raise Conflict(
            f"生图实际返回 {reply.width}x{reply.height}，角色效果图必须是 {width}x{height}"
        )

    params = {**reply.params, "effective_negative_prompt": negative_prompt}
    relative = archive.stage_bytes(
        ref,
        asset_dir=character.dir_name,
        stem=f"{character.name}_{characters.RENDER_SUFFIX}",
        suffix=reply.suffix,
        data=reply.data,
    )
    row = generations.record(
        project,
        target_ref=character.id,
        stage=STAGE,
        file_path=relative,
        file_hash=archive.file_hash(ref.absolute(relative)),
        task_id=character.id,
        asset_spec={**card.as_dict(), "params": params},
    )
    record_event(
        project,
        character.id,
        "render_generated",
        _report(path=relative, size=reply.size_text, params=params),
        {
            "generation_id": row.id,
            "file_path": relative,
            "spec": card.as_dict(),
            "params": params,
        },
    )
    if not characters.at_least(character, characters.RENDER_GENERATED):
        character.state = characters.RENDER_GENERATED
    project.commit()

    archive.merge_meta(
        characters.meta_path(ref, character),
        {
            "render": {
                "state": character.state,
                "generation_id": row.id,
                "file_path": relative,
                "asset_spec": card.as_dict(),
                "params": params,
                "generated_at": datetime.now(UTC).isoformat(),
            }
        },
    )
    characters.sync_meta(ref, character)
    _log.info(
        "render_generated",
        id=character.id,
        generation=row.id,
        path=relative,
        size=reply.size_text,
    )
    return RenderResult(
        character_id=character.id,
        generation_id=row.id,
        file_path=relative,
        spec=card,
        params=params,
        width=reply.width,
        height=reply.height,
    )


# --------------------------------------------------------------------------- #
# 门禁 2：采用
# --------------------------------------------------------------------------- #


def _spec_file_name(row: Generation) -> str:
    name = row.asset_spec.get("file_name") if row.asset_spec else None
    return name if isinstance(name, str) else ""


def adopt(
    project: Session,
    ref: ProjectRef,
    character: Character,
    row: Generation,
    *,
    note: str = "",
) -> archive.ArchiveResult:
    """采用某一张候选：拷到定稿位、台账标定稿、状态推到 S3。

    先落盘再改状态：状态说「渲染图已定稿」的时候，定稿位上必须真的有那张图，否则下一步会
    拿着一个不存在的参考图开工。
    """
    if row.target_ref != character.id or row.stage != STAGE:
        raise Conflict("这条产物不是该角色的渲染图")
    target = characters.render_target(character, _spec_file_name(row))
    result = archive.adopt_file(
        ref,
        source_path=row.file_path,
        target_path=target,
        extra={"stage": STAGE, "generation_id": row.id, "note": note},
    )
    try:
        documents.write_prompt_document(
            ref,
            asset_dir=character.dir_name,
            file_name=layout.RENDER_PROMPT_MD,
            title="效果图提示词",
            generation_id=row.id,
            final_path=result.target_path,
            asset_spec=row.asset_spec or {},
        )
    except Exception:
        archive.rollback_adoption(ref, result)
        raise
    generations.mark_final(
        project, row, file_path=result.target_path, file_hash=result.content_hash
    )
    characters.confirm_render(project, ref, character, render_path=result.target_path, note=note)
    _log.info("render_adopted", id=character.id, generation=row.id, target=result.target_path)
    return result
