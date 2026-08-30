"""四视图评审：`vision_reviewer` 看图裁决，`REJECT` 就把被点名的那几张重生。

跟设定评审同一套骨架（单次调用、裁决全文进 `task_events`、自动重生有上限），差别在三处：

1. **审的是磁盘上那几张**。每个视角取台账里最近的一条，图现场重新量一遍再送审：台账里存的
   是生成当时的读数，而用户可能已经重生过某一张，拿旧读数评审等于评审一张不存在的图。
2. **机器读数一并交给模型**。「背景够不够白」是像素统计题，模型看图判断这一项并不可靠，所以
   边缘白度与非白像素占比由 `imaging` 量出来写进请求里；模型负责数量、分离度、一致性与视角
   这四项它真正擅长的判断。
3. **粒度跟项目的 `review_mode` 走**。`full` 每张单独审（贵，但理由能对上具体那一张）、
   `lean` 四张一次审（默认，一次调用就能看出四个面之间对不对得上）、`solo` 不审（用户自己
   看，平台不拦）。

`REJECT` 之后只重生被点名的视角：四张全重来既多烧三次额度，又会把用户已经认可的三张换掉。
点名靠在「修正建议」那一节里找视角名，找不到就整批重生——宁可多花额度，也不能因为解析不出
名字就把一张不合格的图留在那儿等着进建模。

`APPROVE` 只表示审校没发现问题：状态一步不动，定稿仍要人来选。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from atelier.agents import context, dispatch, parsing, views
from atelier.agents import conversation as conv
from atelier.agents.definitions import get_agent
from atelier.agents.parsing import Verdict, VerdictError
from atelier.assets import archive, characters, imaging, projects
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import Character
from atelier.db.task_events import record as record_event
from atelier.errors import Conflict
from atelier.providers import text_chat

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

## 这一批图（按顺序对应下面附的图）

{shots}

## 机器已经量过的背景数据

{machine}

---

请审上面这 {count} 张图，按你的输出格式回答：首行裁决 token，其下按节写硬性约束逐条、五项
检查清单与修正建议。修正建议里请点明是哪一个视角不合格，平台按你点的名重生那一张。
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
    """每个视角最近那一张，按 `VARIANTS` 的顺序，图现场量一遍。

    顺序固定是给模型看的：请求里第 n 张写的是哪个视角，附的图就得是那一张，否则它给的理由会
    指向错的图，而用户根本看不出这层错位。
    """
    latest = views.latest_by_variant(project, character)
    card = views.base_card(project, character)
    expect = views.image_size(ref, card)

    picked: list[Shot] = []
    for variant in views.VARIANTS:
        row = latest.get(variant.code)
        if row is None:
            continue
        path = ref.absolute(row.file_path)
        if not path.is_file():
            raise Conflict(f"{variant.label}那张图 {row.file_path} 不在磁盘上了，重生一张再评审")
        picked.append(
            Shot(
                variant=variant.code,
                label=variant.label,
                generation_id=row.id,
                file_path=row.file_path,
                report=imaging.measure_file(path, expect=expect),
            )
        )
    if not picked:
        raise Conflict(f"{character.name} 还没有四视图可审，先生成一批")
    return tuple(picked)


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
    """机器读数写成人话。有疑点的连问题一起写，没疑点也要写读数。

    读数一律写出来而不是只报问题：模型看到「边缘白度 99.7%」才知道这一项不用它再判，只报问题
    的话它会把「没提到」理解成「没量过」，然后自己凭观感给一个不可靠的结论。
    """
    lines: list[str] = []
    for one in picked:
        head = (
            f"- {one.label}：尺寸 {one.report.size}，"
            f"边缘白度 {one.report.edge_white:.1%}，非白像素 {one.report.ink:.1%}"
        )
        if one.problems:
            head += "\n" + "\n".join(f"  - 机器判定问题：{problem}" for problem in one.problems)
        lines.append(head)
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
        shots=_shot_lines(picked),
        machine=_machine_lines(picked),
        count=len(picked),
    )
    assembled = context.assemble(
        agent,
        [context.Ask(content=request)],
        addendum=conv.addendum(project, REVIEWER),
        artifact_path=relative if spec is not None else None,
        artifact_text=spec,
        project_memories=conv.enabled_memories(project, character.id),
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


def review_once(
    project: Session,
    runtime: Session,
    ref: ProjectRef,
    character: Character,
    picked: Sequence[Shot],
    *,
    chat: dispatch.ChatFn | None = None,
    attempt: int = 1,
) -> Verdict:
    """调一次 `vision_reviewer`，裁决全文落 `task_events`。

    解析不出裁决时照旧不当成 REJECT：那是格式事故，默认拒收会变成一次没有理由的驳回，用户
    看不出该改 prompt 还是该重试。
    """
    reply = dispatch.run(
        runtime,
        REVIEWER,
        _payload(project, ref, character, picked),
        chat or text_chat.complete,
        project_code=ref.code,
        task_id=character.id,
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


def blamed(verdict: Verdict, picked: Sequence[Shot]) -> tuple[str, ...]:
    """裁决点名了哪几个视角。点不出来就是这一批全部。

    先在「修正建议」那一节里找，找不到再翻全文：建议那一节才是「要改哪张」的地方，全文里
    「正面没问题」这种话也会带上视角名，只按全文匹配会把过关的那张也拖去重生。
    """
    section = next(
        (items for name, items in verdict.sections.items() if ADVICE_SECTION in name), ()
    )
    for scope in ("\n".join(section), verdict.text):
        named = tuple(one.variant for one in picked if _named_in(scope, one.variant))
        if named:
            return named
    return tuple(one.variant for one in picked)


def _named_in(text: str, code: str) -> bool:
    variant = views.BY_CODE[code]
    if variant.label in text or variant.stem in text:
        return True
    # 英文代码得卡词边界：`back` 不卡的话，一句 `pure white background` 就会被当成在点名背面那张
    return re.search(rf"\b{re.escape(variant.code)}\b", text, re.I) is not None


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
    """按 `review_mode` 审这一批四视图，`REJECT` 就重生被点名的那几张再审。

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
        batches = tuple((one,) for one in current) if picked_mode == FULL else ((tuple(current)),)
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
            variants=_regenerate(result, current),
            generate=generate,
        )
        attempt += 1


def _regenerate(result: VisionResult, picked: Sequence[Shot]) -> tuple[views.Variant, ...]:
    """要重生哪几个视角：被 REJECT 的那几批里点名的那些。"""
    named: list[str] = []
    by_code = {one.variant: one for one in picked}
    for one in result.verdicts:
        if not one.verdict.rejected:
            continue
        scope = [by_code[code] for code in one.variants if code in by_code]
        named.extend(code for code in blamed(one.verdict, scope) if code not in named)
    return tuple(views.BY_CODE[code] for code in named if code in views.BY_CODE)


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
