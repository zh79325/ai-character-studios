"""角色接口：建角色、自动评审、人工门禁、状态推进。

门禁 1 落在这里：设定没确认，后续每一步都进不去（`POST /advance` 直接 409）。这道拦阻不是
另写一层校验，而是状态机本身——`characters.advance` 只允许一步一步往前，没过 S1 就推不到
S2。API 只负责把它的 `Conflict` 翻成 409。

评审与门禁是两回事：`POST /review` 只给出裁决与理由，`POST /spec/confirm` 才是放行。哪怕
裁决是 `APPROVE` 也得人按一下——自动裁决替人拍板，等于把责任推给一个看不见全局的模型。

一次评审是**同步阻塞**的（`def` 而非 `async def`，FastAPI 放线程池跑）：用户按了「评审」就
在等这一轮结果，拆成提交任务 + 轮询只是把复杂度转给前端。中途的增量走会话那条 SSE。
"""

from __future__ import annotations

from fastapi import APIRouter, status

from atelier.agents import conversation as engine
from atelier.agents import review as reviewer
from atelier.api.deps import CurrentProject, ProjectDb, RuntimeDb
from atelier.api.schemas import (
    AdvanceIn,
    CharacterCreateIn,
    CharacterOut,
    ConstraintOut,
    GateIn,
    SpecReviewIn,
    SpecReviewOut,
    TaskEventOut,
)
from atelier.assets import characters, projects
from atelier.db import task_events
from atelier.db.project_models import TaskEvent

router = APIRouter(prefix="/api/characters", tags=["characters"])


def character_out(row: dict[str, object]) -> CharacterOut:
    """把 `projects.character_row` 的那份 dict 补上人话状态再出去。

    列表与详情共用同一个构造口：两处各拼一遍的话，加字段时总会只加到其中一边。
    """
    state = str(row["state"])
    return CharacterOut.model_validate({**row, "state_label": characters.label(state)})


def _event_out(row: TaskEvent) -> TaskEventOut:
    return TaskEventOut(
        seq=row.seq,
        ts=row.ts.isoformat(),
        level=row.level,
        event=row.event,
        message=row.message,
        payload=row.payload,
    )


@router.post("", response_model=CharacterOut, status_code=status.HTTP_201_CREATED)
def create_character(
    body: CharacterCreateIn, project: ProjectDb, ref: CurrentProject
) -> CharacterOut:
    """建角色。项目还没聊出 art bible 就不给建（409）。

    art bible 是角色设定的风格锚点，拿一份模板原样当锚点等于没有锚点，后面每张图都会跑偏。
    """
    character = characters.create(project, ref, body.name)
    return character_out(projects.character_row(character))


@router.get("/{character_id}", response_model=CharacterOut)
def get_character(character_id: str, project: ProjectDb) -> CharacterOut:
    return character_out(projects.character_row(characters.get(project, character_id)))


@router.get("/{character_id}/events", response_model=list[TaskEventOut])
def list_events(character_id: str, project: ProjectDb) -> list[TaskEventOut]:
    """这个角色的事件时间线：裁决全文、门禁拍板与理由都在这里。

    事后要回答「这份定稿当时凭什么过的」，只有这条线答得上。
    """
    characters.get(project, character_id)
    return [_event_out(row) for row in task_events.history(project, character_id)]


@router.post("/{character_id}/review", response_model=SpecReviewOut)
def review_spec(
    character_id: str,
    body: SpecReviewIn,
    project: ProjectDb,
    runtime: RuntimeDb,
    ref: CurrentProject,
) -> SpecReviewOut:
    """让 `spec_reviewer` 审最新那一版设定。草稿优先于磁盘定稿。

    带上 `conversation_id` 才会在 `REJECT` 后自动把理由发回写手重生：重生得有个会话承载新
    的一轮，不然这几轮对话跟用户自己那场对不上号。
    """
    character = characters.get(project, character_id)
    conversation = (
        engine.get(project, body.conversation_id) if body.conversation_id is not None else None
    )
    result = reviewer.review(project, runtime, ref, character, conversation=conversation)
    verdict = result.verdict
    return SpecReviewOut(
        character_id=result.character_id,
        decision=result.decision,
        approved=result.approved,
        attempt=result.attempt,
        regenerated=result.regenerated,
        manual=result.manual,
        sections={name: list(items) for name, items in verdict.sections.items()},
        constraints=[ConstraintOut(item=one.item, value=one.value) for one in verdict.constraints],
        text=verdict.text,
    )


@router.post("/{character_id}/spec/confirm", response_model=CharacterOut)
def confirm_spec(
    character_id: str, body: GateIn, project: ProjectDb, ref: CurrentProject
) -> CharacterOut:
    """门禁 1：人工确认设定，推到 S1。

    确认的是磁盘上那一份。`spec_path` 还空着说明用户没按过「确认沉淀」，这时候放行会让后续
    每一步都拿不到设定原文。
    """
    character = characters.confirm_spec(
        project, ref, characters.get(project, character_id), note=body.note
    )
    return character_out(projects.character_row(character))


@router.post("/{character_id}/spec/reject", response_model=CharacterOut)
def reject_spec(character_id: str, body: GateIn, project: ProjectDb) -> CharacterOut:
    """门禁 1 驳回：状态停在原地，理由记进时间线。

    驳回不是一个新阶段，是「这一步还没过」。理由留着，下一轮设定会话能看见上次为什么没过。
    """
    character = characters.reject_spec(
        project, characters.get(project, character_id), note=body.note
    )
    return character_out(projects.character_row(character))


@router.post("/{character_id}/advance", response_model=CharacterOut)
def advance(
    character_id: str, body: AdvanceIn, project: ProjectDb, ref: CurrentProject
) -> CharacterOut:
    """推进状态。只能往前，且一步一步走。

    设定没确认就想推到「渲染图已生成」会拿到 409：状态是后续每一步的凭据，能任意改写的凭据
    等于没有凭据。
    """
    character = characters.advance(project, ref, characters.get(project, character_id), body.state)
    return character_out(projects.character_row(character))
