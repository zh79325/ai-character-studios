"""设定评审：一次 `spec_reviewer` 单次调用，加上 REJECT 时的自动重生。

审的是**最新那一版**：草稿还挂在 `artifact_drafts` 里就审草稿，没有草稿才审磁盘上的定
稿。反过来只认磁盘那份的话，用户刚跟 `spec_writer` 聊出来的新版根本进不了评审，而评审通过
恰恰是他决定要不要沉淀的依据。

裁决全文原样写进 `task_events`：日后要回答「这份设定当时凭什么过的」，只有当时那段理由答
得上，摘成一句「CONCERNS 2 处」等于把证据丢了。

`REJECT` 自动把理由发回 `spec_writer` 重生，上限 `MAX_AUTO_REGENERATIONS` 次。设上限是因
为模型反复被同一份 art bible 卡住时，第四次多半还是同样的答案，继续烧 token 不如把问题摆
给用户看——他改一句 art bible 就能解开，模型自己解不开。

`APPROVE` 只表示审校没发现问题，状态与门禁一步都不动：放行是人的事，这里连 `state` 都不碰。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from atelier.agents import context, dispatch, parsing
from atelier.agents import conversation as conv
from atelier.agents.definitions import get_agent
from atelier.agents.parsing import Verdict, VerdictError
from atelier.assets import characters, projects
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import ArtifactDraft, Character, Conversation
from atelier.db.task_events import record as record_event
from atelier.errors import Conflict
from atelier.providers import text_chat

_log = structlog.get_logger(__name__)

REVIEWER = "spec_reviewer"

MAX_AUTO_REGENERATIONS = 3
"""REJECT 后最多自动重生几次。到顶就转人工，不再无声地烧 token。"""

CONSTRAINTS_SKIPPED = (parsing.CONSTRAINTS_SECTION,)
"""重生时不回传的节：约束清单是给后续生图用的产出，不是要 `spec_writer` 改的问题。"""

REVIEW_REQUEST = """## 项目视觉规范（art-bible.md）

{art_bible}

---

请审校上面「当前定稿全文」里的设定（角色：{name}），按你的输出格式回答：首行裁决 token，
其下按节写缺失维度、模糊表述、art bible 冲突与硬性约束清单。
"""

RETRY_REQUEST = """审校没通过（第 {attempt} 次），问题如下：

{reasons}

请据此改这一版设定，只改被指出的地方，其余保持原样，改完仍按原格式整篇输出草稿。
"""


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """一次评审的结果。前端据此展示裁决卡片，API 据此决定给不给人工门禁按钮。"""

    character_id: str
    verdict: Verdict
    attempt: int
    """这次是第几轮评审（含自动重生）。"""
    regenerated: int = 0
    """自动重生了几次。"""
    manual: bool = False
    """要不要转人工：自动重生用尽仍未通过。"""

    @property
    def decision(self) -> str:
        return self.verdict.decision

    @property
    def approved(self) -> bool:
        return self.verdict.approved


def latest_spec(project: Session, ref: ProjectRef, character: Character) -> tuple[str, str]:
    """要审的那一版设定：优先未确认的最新草稿，其次磁盘上的定稿。"""
    relative = characters.spec_target(character)
    draft = project.scalars(
        select(ArtifactDraft)
        .where(ArtifactDraft.target_path == relative, ArtifactDraft.status == "pending")
        .order_by(ArtifactDraft.created_at.desc())
        .limit(1)
    ).one_or_none()
    if draft is not None:
        return relative, draft.content

    path = ref.absolute(relative)
    if path.is_file():
        return relative, path.read_text(encoding="utf-8")
    raise Conflict(f"{character.name} 还没有设定内容可审，先在设定会话里聊出一版草稿")


@dataclass(slots=True)
class _Ask:
    """喂给 `context.assemble` 的单条用户消息。

    单次调用没有对话历史，但上下文的拼装顺序（提示词 → 定稿 → 项目记忆）跟会话完全一样，
    没必要另写一套拼法。
    """

    content: str
    turn_no: int = 1
    role: str = "user"
    folded: bool = False


def _payload(
    project: Session, ref: ProjectRef, character: Character
) -> tuple[list[dict[str, str]], str]:
    agent = get_agent(REVIEWER)
    relative, spec = latest_spec(project, ref, character)
    request = REVIEW_REQUEST.format(
        art_bible=projects.read_art_bible(ref).strip() or "（这个项目还没写视觉规范）",
        name=character.name,
    )
    assembled = context.assemble(
        agent,
        [_Ask(content=request)],
        addendum=conv.addendum(project, REVIEWER),
        artifact_path=relative,
        artifact_text=spec,
        project_memories=conv.enabled_memories(project, character.id),
    )
    return assembled.payload(), relative


def review_once(
    project: Session,
    runtime: Session,
    ref: ProjectRef,
    character: Character,
    *,
    chat: dispatch.ChatFn | None = None,
    attempt: int = 1,
) -> Verdict:
    """调一次 `spec_reviewer`，把裁决全文与约束清单落库。

    解析不出裁决时不当成 REJECT：那是模型的格式事故，默认拒收会变成一次没有理由的驳回，
    用户看不出该改设定还是该重试。事故照样记一条事件，抛给上层去决定重试。
    """
    payload, relative = _payload(project, ref, character)
    reply = dispatch.run(
        runtime,
        REVIEWER,
        payload,
        chat or text_chat.complete,
        project_code=ref.code,
        task_id=character.id,
    )
    text = reply.content.strip()

    try:
        verdict = parsing.parse_verdict(text, parsing.SPEC_CHECK)
    except VerdictError as exc:
        record_event(
            project,
            character.id,
            "spec_review_unparsable",
            str(exc),
            {"attempt": attempt, "reply": text[:2000]},
            level="error",
        )
        project.commit()
        raise

    record_event(
        project,
        character.id,
        "spec_reviewed",
        text,
        {
            "attempt": attempt,
            "decision": verdict.decision,
            "spec_path": relative,
            "sections": {name: list(items) for name, items in verdict.sections.items()},
            "constraints": _as_items(verdict),
        },
        level="info" if verdict.approved else "warning",
    )
    _store_constraints(ref, character, verdict, spec_path=relative)
    project.commit()
    _log.info(
        "spec_reviewed",
        id=character.id,
        decision=verdict.decision,
        attempt=attempt,
        constraints=len(verdict.constraints),
    )
    return verdict


def _as_items(verdict: Verdict) -> list[dict[str, str]]:
    return [{"item": one.item, "value": one.value} for one in verdict.constraints]


def _store_constraints(
    ref: ProjectRef, character: Character, verdict: Verdict, *, spec_path: str
) -> None:
    """约束清单回填库行并同步进 `meta.json`。

    整份覆盖而不是并进旧的：清单是对**这一版设定**的翻译，设定改过之后旧条目就是错的——留
    着它，后续每张图都会拿一条已经不成立的要求去判定。
    """
    character.hard_constraints = {
        "decision": verdict.decision,
        "spec_path": spec_path,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "items": _as_items(verdict),
    }
    characters.sync_meta(ref, character)


def reasons_of(verdict: Verdict) -> str:
    """把裁决理由拼回一段人话，发给 `spec_writer` 改。

    只回传问题、不回传硬性约束清单：清单是评审给后续生图的产出，混进来会让写手把「尾巴 = 2
    条」抄成设定里的一句话，而不是去补它真正缺的那一节。
    """
    blocks: list[str] = []
    for name, items in verdict.sections.items():
        if not items or any(skip in name for skip in CONSTRAINTS_SKIPPED):
            continue
        listed = "\n".join(f"- {one}" for one in items)
        blocks.append(f"### {name}\n{listed}")
    return "\n\n".join(blocks) or "（审校没给出具体条目，请自查七个必填维度是否齐全）"


def review(
    project: Session,
    runtime: Session,
    ref: ProjectRef,
    character: Character,
    *,
    conversation: Conversation | None = None,
    chat: dispatch.ChatFn | None = None,
) -> ReviewResult:
    """评审一版设定，`REJECT` 就把理由发回设定会话重生，至多 `MAX_AUTO_REGENERATIONS` 次。

    没给会话就只审一次——重生得有个会话承载新的一轮，硬造一个的话这几轮对话会跟用户自己那
    场对不上号，他在界面上看不到设定是怎么变成现在这样的。
    """
    attempt = 1
    regenerated = 0

    while True:
        verdict = review_once(project, runtime, ref, character, chat=chat, attempt=attempt)
        if not verdict.rejected or conversation is None:
            return ReviewResult(
                character_id=character.id,
                verdict=verdict,
                attempt=attempt,
                regenerated=regenerated,
            )
        if regenerated >= MAX_AUTO_REGENERATIONS:
            record_event(
                project,
                character.id,
                "spec_review_manual",
                f"自动重生 {regenerated} 次仍未通过，转人工",
                {"attempt": attempt, "decision": verdict.decision},
                level="warning",
            )
            project.commit()
            return ReviewResult(
                character_id=character.id,
                verdict=verdict,
                attempt=attempt,
                regenerated=regenerated,
                manual=True,
            )

        regenerated += 1
        conv.send(
            project,
            runtime,
            ref,
            conversation,
            RETRY_REQUEST.format(attempt=attempt, reasons=reasons_of(verdict)),
            chat=chat or text_chat.complete,
            stream=False,
        )
        attempt += 1
