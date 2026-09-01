"""单张四视图四宫格评审：机器逐格检查，`vision_reviewer` 整图裁决。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from atelier.agents import audit, context, dispatch, parsing, views
from atelier.agents import conversation as conv
from atelier.agents.definitions import get_agent
from atelier.agents.parsing import Verdict, VerdictError
from atelier.assets import archive, characters, generations, imaging, projects
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import Character
from atelier.db.task_events import record as record_event
from atelier.errors import Conflict
from atelier.providers import text_chat
from atelier.providers.base import Decision

_log = structlog.get_logger(__name__)

REVIEWER = "vision_reviewer"

MAX_AUTO_REGENERATIONS = 3
"""REJECT 后最多自动重生几批。到顶转人工——模型连着三次都过不了的问题，多半得人改 prompt
或换姿势模版才解得开。"""

FULL = "full"
LEAN = "lean"
SOLO = "solo"

SEVERITY = {"APPROVE": 0, "CONCERNS": 1, "REJECT": 2}
"""一批里取最严的那一档当整批结论：四个面只要有一个不能用，这一组就不能进建模。"""

ADVICE_SECTION = "修正建议"

REVIEW_REQUEST = """## 硬性约束清单（逐条比对，一条都不能跳）

{constraints}

## 四宫格布局

这是一张 2048×2048 的 2×2 四视图：左上正面、右上右侧 30°、左下背面、右下左侧 30°。

## 机器逐格检查结果

{machine}

---

请只审这一张四宫格。逐格确认位置与视角正确、四个角色造型一致、硬性约束均满足，且每格只有
一个完整角色。人物不得出现披风、斗篷、披肩、长袍、长外套、垂布、飘带或其他遮挡躯干和四肢
轮廓的服装。任意一格不合格都应 REJECT，并在修正建议中点明格位。
"""

RENDER_REVIEW_REQUEST = """## 用户检查要求

{note}

## 硬性约束清单（逐条比对）

{constraints}

---

请审查这张角色效果图。只检查角色特征、附属结构数量与分离度、服装是否遮挡身体轮廓，
不要求纯色背景或标准视角。给出约定的 VIEW-CHECK 裁决；裁决只能提供建议，不能替用户定稿。
"""

NO_CONSTRAINTS = "（这个角色还没有硬性约束清单，只按五项检查清单判）"


@dataclass(frozen=True, slots=True)
class Shot:
    """送审的一张图：台账里那一条 + 现场量出来的读数。"""

    variant: str
    label: str
    generation_id: str
    file_path: str
    report: imaging.Report

    @property
    def problems(self) -> tuple[str, ...]:
        return self.report.problems


@dataclass(frozen=True, slots=True)
class ViewVerdict:
    """一次调用的裁决，以及它审的是哪几个视角。"""

    variants: tuple[str, ...]
    verdict: Verdict

    @property
    def decision(self) -> str:
        return self.verdict.decision


@dataclass(frozen=True, slots=True)
class VisionResult:
    """一轮四视图评审的结果。前端据此展示裁决卡片，人工据此决定要不要定稿。"""

    character_id: str
    mode: str
    verdicts: tuple[ViewVerdict, ...]
    attempt: int
    regenerated: int = 0
    manual: bool = False
    """自动重生用尽仍未通过，得人来看。"""

    skipped: bool = False
    """`solo` 模式没调用评审。"""

    @property
    def decision(self) -> str:
        """整批结论：取最严的那一档。没审就是 APPROVE 之外的第三种情况，用空串表示。"""
        if not self.verdicts:
            return ""
        return max((one.decision for one in self.verdicts), key=lambda one: SEVERITY.get(one, 0))

    @property
    def approved(self) -> bool:
        return self.decision == "APPROVE"

    @property
    def rejected(self) -> bool:
        return self.decision == "REJECT"


# --------------------------------------------------------------------------- #
# 送审的那几张
# --------------------------------------------------------------------------- #


def mode_of(ref: ProjectRef) -> str:
    return projects.read_config(ref.dir).review_mode


def shots(project: Session, ref: ProjectRef, character: Character) -> tuple[Shot, ...]:
    """取最新一张四宫格，旧版四张分图只读保留，不再进入新评审。"""
    row = views.latest_sheet(project, character)
    if row is None:
        raise Conflict(f"{character.name} 还没有新版四视图四宫格，请重新生成")
    path = ref.absolute(row.file_path)
    if not path.is_file():
        raise Conflict(f"四视图 {row.file_path} 不在磁盘上了，重新生成一张再评审")
    card = views.base_card(project, character)
    return (
        Shot(
            variant=views.SHEET_CODE,
            label=views.SHEET_LABEL,
            generation_id=row.id,
            file_path=row.file_path,
            report=imaging.measure_grid(
                path.read_bytes(), background_color=str(card["view_background_color"])
            ),
        ),
    )


def constraint_lines(character: Character) -> str:
    items = [
        f"- {one.get('item', '').strip()} = {one.get('value', '').strip()}"
        for one in characters.hard_constraints(character)
        if one.get("item")
    ]
    return "\n".join(items) or NO_CONSTRAINTS


def _shot_lines(picked: Sequence[Shot]) -> str:
    return "\n".join(
        f"- 第 {index} 张：{one.label}（{one.file_path}）" for index, one in enumerate(picked, 1)
    )


def _machine_lines(picked: Sequence[Shot]) -> str:
    one = picked[0]
    lines = [
        f"- 整图：尺寸 {one.report.size}，目标背景 {one.report.target_color}，"
        f"透明像素 {one.report.transparent:.1%}"
    ]
    lines.extend(
        f"- {region.label}：边缘匹配率 {region.edge_match:.1%}，"
        f"透明像素 {region.transparent:.1%}，主体像素 {region.subject:.1%}"
        + (
            "\n" + "\n".join(f"  - 机器判定问题：{problem}" for problem in region.problems)
            if region.problems
            else ""
        )
        for region in one.report.regions
    )
    return "\n".join(lines)


def _payload(
    project: Session, ref: ProjectRef, character: Character, picked: Sequence[Shot]
) -> list[dict[str, Any]]:
    """组这一次的消息：系统提示词 + 设定原文 + 请求，最后一条带上图。

    设定原文照旧挂在 artifact 位上：五项里的「角色一致性」要对着设定判，只给图的话它只能凭
    渲染图之间像不像来判断，而四张都跑偏时它们之间恰恰是自洽的。
    """
    agent = get_agent(REVIEWER)
    relative = characters.spec_target(character)
    spec_path = ref.absolute(relative)
    spec = spec_path.read_text(encoding="utf-8") if spec_path.is_file() else None
    request = REVIEW_REQUEST.format(
        constraints=constraint_lines(character),
        machine=_machine_lines(picked),
    )
    assembled = context.assemble(
        agent,
        [context.Ask(content=request)],
        addendum=conv.addendum(ref, REVIEWER),
        artifact_path=relative if spec is not None else None,
        artifact_text=spec,
        project_memories=conv.enabled_memories(project, ref, character.id),
    )
    messages = assembled.payload()
    last = messages[-1]
    return [
        *messages[:-1],
        text_chat.vision_message(
            str(last["content"]),
            [ref.absolute(one.file_path) for one in picked],
            role=str(last.get("role", "user")),
        ),
    ]


# --------------------------------------------------------------------------- #
# 单次调用
# --------------------------------------------------------------------------- #


def _call_reviewer(
    runtime: Session,
    ref: ProjectRef,
    character: Character,
    payload: list[dict[str, Any]],
    *,
    chat: dispatch.ChatFn | None,
    decision: Decision | None,
    reselect: dispatch.Reselect | None,
    turn_audit: audit.TurnAudit | None,
    purpose: str,
) -> text_chat.ChatReply:
    caller = chat or text_chat.complete
    if decision is None:
        return dispatch.run(
            runtime,
            REVIEWER,
            payload,
            caller,
            project_code=ref.code,
            task_id=character.id,
        )
    agent = get_agent(REVIEWER)
    max_tokens = text_chat.output_budget(decision.candidate, agent.max_output_tokens)
    if turn_audit is not None:
        turn_audit.write_request(purpose, decision.candidate, payload, max_tokens=max_tokens)
    try:
        reply = dispatch.call(
            runtime,
            REVIEWER,
            decision,
            payload,
            caller,
            project_code=ref.code,
            task_id=character.id,
            reselect=reselect,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        if turn_audit is not None:
            turn_audit.write_error(exc)
        raise
    if turn_audit is not None:
        turn_audit.write_response(reply)
    return reply


def review_once(
    project: Session,
    runtime: Session,
    ref: ProjectRef,
    character: Character,
    picked: Sequence[Shot],
    *,
    chat: dispatch.ChatFn | None = None,
    attempt: int = 1,
    decision: Decision | None = None,
    reselect: dispatch.Reselect | None = None,
    turn_audit: audit.TurnAudit | None = None,
) -> Verdict:
    """调一次 `vision_reviewer`，裁决全文落 `task_events`。

    解析不出裁决时照旧不当成 REJECT：那是格式事故，默认拒收会变成一次没有理由的驳回，用户
    看不出该改 prompt 还是该重试。
    """
    reply = _call_reviewer(
        runtime,
        ref,
        character,
        _payload(project, ref, character, picked),
        chat=chat,
        decision=decision,
        reselect=reselect,
        turn_audit=turn_audit,
        purpose=f"评审四视图（第 {attempt} 次）",
    )
    text = reply.content.strip()
    covered = [one.variant for one in picked]

    try:
        verdict = parsing.parse_verdict(text, parsing.VIEW_CHECK)
    except VerdictError as exc:
        record_event(
            project,
            character.id,
            "views_review_unparsable",
            str(exc),
            {"attempt": attempt, "variants": covered, "reply": text[:2000]},
            level="error",
        )
        project.commit()
        raise

    record_event(
        project,
        character.id,
        "views_reviewed",
        text,
        {
            "attempt": attempt,
            "decision": verdict.decision,
            "variants": covered,
            "generation_ids": [one.generation_id for one in picked],
            "machine_problems": {one.variant: list(one.problems) for one in picked},
            "sections": {name: list(items) for name, items in verdict.sections.items()},
        },
        level="info" if verdict.approved else "warning",
    )
    project.commit()
    _log.info(
        "views_reviewed",
        id=character.id,
        decision=verdict.decision,
        variants=covered,
        attempt=attempt,
    )
    return verdict


def review_render(
    project: Session,
    runtime: Session,
    ref: ProjectRef,
    character: Character,
    *,
    note: str = "",
    chat: dispatch.ChatFn | None = None,
    decision: Decision | None = None,
    reselect: dispatch.Reselect | None = None,
    turn_audit: audit.TurnAudit | None = None,
) -> Verdict:
    """审查最新效果图，结论只供人工门禁参考，不推进角色状态。"""
    row = generations.latest(project, target_ref=character.id, stage=generations.RENDER)
    if row is None:
        raise Conflict(f"{character.name} 还没有效果图，请先生成一张")
    image_path = ref.absolute(row.file_path)
    if not image_path.is_file():
        raise Conflict(f"效果图 {row.file_path} 不在磁盘上了，请重新生成")
    relative = characters.spec_target(character)
    spec_path = ref.absolute(relative)
    spec = spec_path.read_text(encoding="utf-8") if spec_path.is_file() else None
    request = RENDER_REVIEW_REQUEST.format(
        note=note.strip() or "请按当前设定检查",
        constraints=constraint_lines(character),
    )
    assembled = context.assemble(
        get_agent(REVIEWER),
        [context.Ask(content=request)],
        addendum=conv.addendum(ref, REVIEWER),
        artifact_path=relative if spec is not None else None,
        artifact_text=spec,
        project_memories=conv.enabled_memories(project, ref, character.id),
    )
    messages = assembled.payload()
    last = messages[-1]
    payload = [
        *messages[:-1],
        text_chat.vision_message(
            str(last["content"]),
            [image_path],
            role=str(last.get("role", "user")),
        ),
    ]
    reply = _call_reviewer(
        runtime,
        ref,
        character,
        payload,
        chat=chat,
        decision=decision,
        reselect=reselect,
        turn_audit=turn_audit,
        purpose="评审效果图",
    )
    text = reply.content.strip()
    verdict = parsing.parse_verdict(text, parsing.VIEW_CHECK)
    record_event(
        project,
        character.id,
        "render_reviewed",
        text,
        {
            "decision": verdict.decision,
            "generation_id": row.id,
            "sections": {name: list(items) for name, items in verdict.sections.items()},
        },
        level="info" if verdict.approved else "warning",
    )
    project.commit()
    return verdict


def blamed(_verdict: Verdict, _picked: Sequence[Shot]) -> tuple[str, ...]:
    """四宫格任一格不合格都必须整张重生。"""
    return (views.SHEET_CODE,)


# --------------------------------------------------------------------------- #
# 一轮评审（含自动重生）
# --------------------------------------------------------------------------- #


def review(
    project: Session,
    runtime: Session,
    ref: ProjectRef,
    character: Character,
    *,
    chat: dispatch.ChatFn | None = None,
    generate: dispatch.ImageFn | None = None,
    mode: str | None = None,
) -> VisionResult:
    """按 `review_mode` 整图评审四宫格，`REJECT` 就重生整张后再审。

    `generate` 不给就只审不重生：重生要花额度，调用方（API、CLI）得明确表示它接受这笔开销。
    """
    characters.require_state(character, characters.VIEWS_GENERATED, action="评审四视图")
    picked_mode = mode or mode_of(ref)
    if picked_mode == SOLO:
        record_event(
            project,
            character.id,
            "views_review_skipped",
            "项目 review_mode 是 solo，四视图不过自动评审，由人自己看",
            {"mode": picked_mode},
        )
        project.commit()
        return VisionResult(
            character_id=character.id, mode=picked_mode, verdicts=(), attempt=0, skipped=True
        )

    attempt = 1
    regenerated = 0
    while True:
        current = shots(project, ref, character)
        batches = (tuple(current),)
        verdicts = tuple(
            ViewVerdict(
                variants=tuple(one.variant for one in batch),
                verdict=review_once(
                    project, runtime, ref, character, batch, chat=chat, attempt=attempt
                ),
            )
            for batch in batches
        )
        result = VisionResult(
            character_id=character.id,
            mode=picked_mode,
            verdicts=verdicts,
            attempt=attempt,
            regenerated=regenerated,
        )
        _write_meta(ref, character, result)

        if not result.rejected or generate is None:
            return result
        if regenerated >= MAX_AUTO_REGENERATIONS:
            record_event(
                project,
                character.id,
                "views_review_manual",
                f"自动重生 {regenerated} 次仍未通过，转人工",
                {"attempt": attempt, "decision": result.decision},
                level="warning",
            )
            project.commit()
            manual = VisionResult(
                character_id=character.id,
                mode=picked_mode,
                verdicts=verdicts,
                attempt=attempt,
                regenerated=regenerated,
                manual=True,
            )
            _write_meta(ref, character, manual)
            return manual

        regenerated += 1
        views.generate_views(
            project,
            runtime,
            ref,
            character,
            generate=generate,
        )
        attempt += 1


def _write_meta(ref: ProjectRef, character: Character, result: VisionResult) -> None:
    """最近一次评审结论进 `meta.json` 的 `views.review`。

    只并 `views` 里的这一个子键：同一段还存着生成时的参数快照，写评审的这一方不该把它抹掉。
    """
    path = characters.meta_path(ref, character)
    meta = archive.read_meta(path)
    section = dict(meta.get("views") or {})
    section["review"] = {
        "mode": result.mode,
        "decision": result.decision,
        "attempt": result.attempt,
        "regenerated": result.regenerated,
        "manual": result.manual,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "verdicts": [
            {
                "variants": list(one.variants),
                "decision": one.decision,
                "text": one.verdict.text,
            }
            for one in result.verdicts
        ],
    }
    archive.merge_meta(path, {"views": section})
