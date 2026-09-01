"""角色接口：建角色、自动评审、人工门禁、生渲染图、状态推进。

两道人工门禁都落在这里，形状一致：评审/生成只给结果，confirm 才是放行。设定没确认，后续
每一步都进不去（出卡片、生图、`POST /advance` 一律 409）。这道拦阻不是另写一层校验，而是
状态机本身——`characters.require_state` 与 `advance` 只认状态，API 只负责把 `Conflict` 翻成 409。

评审与门禁是两回事：`POST /review` 只给出裁决与理由，`POST /spec/confirm` 才是放行。哪怕
裁决是 `APPROVE` 也得人按一下——自动裁决替人拍板，等于把责任推给一个看不见全局的模型。

评审与生图都是**同步阻塞**的（`def` 而非 `async def`，FastAPI 放线程池跑）：用户按了就在等
这一轮结果，拆成提交任务 + 轮询只是把复杂度转给前端。中途的增量走会话那条 SSE。
"""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import FileResponse

from atelier.agents import conversation as engine
from atelier.agents import render as painter
from atelier.agents import review as reviewer
from atelier.agents import views, vision
from atelier.agents.parsing import AssetSpec
from atelier.api.deps import CurrentProject, ProjectDb, RuntimeDb
from atelier.api.schemas import (
    AdvanceIn,
    AssetSpecIn,
    AssetSpecOut,
    CharacterCreateIn,
    CharacterOut,
    ConstraintOut,
    GateIn,
    GenerationOut,
    RenderAdoptIn,
    RenderIn,
    RenderOut,
    SpecReviewIn,
    SpecReviewOut,
    TaskEventOut,
    ViewFailureOut,
    ViewImageOut,
    ViewReviewIn,
    ViewReviewOut,
    ViewsAdoptIn,
    ViewSetOut,
    ViewsIn,
    ViewVerdictOut,
)
from atelier.assets import characters, generations, projects
from atelier.db import task_events
from atelier.db.project_models import Character, Generation, TaskEvent
from atelier.errors import Conflict, NotFound
from atelier.providers import image_gen

router = APIRouter(prefix="/api/projects/{project_code}/characters", tags=["characters"])


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
    character = characters.create(project, ref, body.name, body.group, body.overwrite)
    return character_out(projects.character_row(character))


@router.delete("/{character_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_missing_character(character_id: str, project: ProjectDb, ref: CurrentProject) -> None:
    """删除磁盘扫描已确认缺失的角色记录；仍有角色目录时拒绝。"""
    characters.remove_missing(project, ref, character_id)


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


def _spec_out(spec: AssetSpec) -> AssetSpecOut:
    return AssetSpecOut.model_validate(spec.as_dict())


def _generation_out(row: Generation) -> GenerationOut:
    return GenerationOut(
        id=row.id,
        stage=row.stage,
        variant=row.variant,
        file_path=row.file_path,
        file_hash=row.file_hash,
        is_final=row.is_final,
        created_at=row.created_at.isoformat(),
        asset_spec=row.asset_spec,
    )


@router.post("/{character_id}/asset-spec", response_model=AssetSpecOut)
def draft_asset_spec(
    character_id: str,
    body: AssetSpecIn,
    project: ProjectDb,
    runtime: RuntimeDb,
    ref: CurrentProject,
) -> AssetSpecOut:
    """让 `prompt_smith` 出一张渲染图卡片，不生图。

    卡片先给人看一眼是有必要的：卡片里的 prompt 就是这张图的全部依据，层序缺一截用户在图上只
    能看出「不对」而看不出「哪里不对」。设定没确认就 409——它是翻译的底本。
    """
    character = characters.get(project, character_id)
    spec = painter.make_spec(project, runtime, ref, character, note=body.note, field=body.field)
    return _spec_out(spec)


@router.post("/{character_id}/render", response_model=RenderOut)
def render_character(
    character_id: str,
    body: RenderIn,
    project: ProjectDb,
    runtime: RuntimeDb,
    ref: CurrentProject,
) -> RenderOut:
    """出一张渲染图：先拿卡片再生图，产物落 `tmp/`，状态推到 S2。

    同步阻塞（`def` 而非 `async def`，FastAPI 放线程池跑）：用户按了就在等这张图，拆成提交
    任务 + 轮询只是把复杂度转给前端。

    `field` 给了就是「改某一项重生」：只把那一项发回给写手，其余字段与 prompt 的其他层原样留着。
    """
    character = characters.get(project, character_id)
    result = painter.render(project, runtime, ref, character, note=body.note, field=body.field)
    return RenderOut(
        character_id=result.character_id,
        generation_id=result.generation_id,
        file_path=result.file_path,
        width=result.width,
        height=result.height,
        spec=_spec_out(result.spec),
        params=result.params,
    )


@router.get("/{character_id}/renders", response_model=list[GenerationOut])
def list_renders(character_id: str, project: ProjectDb) -> list[GenerationOut]:
    """渲染图的全部候选，新的在前。门禁上要在几张之间挑，就得能把过往那几张一并列出来。"""
    characters.get(project, character_id)
    rows = generations.candidates(project, target_ref=character_id, stage=painter.STAGE)
    return [_generation_out(row) for row in rows]


@router.get("/{character_id}/renders/{generation_id}/image")
def read_render(
    character_id: str, generation_id: str, project: ProjectDb, ref: CurrentProject
) -> FileResponse:
    """把图本体发出去。

    不把图转 base64 塞进 JSON：一张 2048 的 png 动辄几 MB，进 JSON 再膨 33%，而浏览器对
    `<img src>` 本来就会缓存与断点续传。
    """
    characters.get(project, character_id)
    row = generations.get(project, generation_id)
    if row is None or row.target_ref != character_id:
        raise NotFound(f"产物 {generation_id} 不属于这个角色")
    path = ref.absolute(row.file_path)
    if not path.is_file():
        raise NotFound(f"{row.file_path} 不在磁盘上了")
    media = image_gen.MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, filename=path.name)


@router.post("/{character_id}/render/confirm", response_model=CharacterOut)
def confirm_render(
    character_id: str, body: RenderAdoptIn, project: ProjectDb, ref: CurrentProject
) -> CharacterOut:
    """门禁 2：采用指定的那一张，拷到定稿位并推到 S3。

    要指名 `generation_id`：默认采用「最新一张」在用户连生了几张之后就不是他指的那一张了。
    """
    character = characters.get(project, character_id)
    row = generations.get(project, body.generation_id)
    if row is None:
        raise NotFound(f"产物 {body.generation_id} 不存在")
    painter.adopt(project, ref, character, row, note=body.note)
    return character_out(projects.character_row(character))


@router.post("/{character_id}/render/reject", response_model=CharacterOut)
def reject_render(character_id: str, body: GateIn, project: ProjectDb) -> CharacterOut:
    """门禁 2 驳回：状态停在 S2，理由记进时间线给下一轮重生用。"""
    character = characters.reject_render(
        project, characters.get(project, character_id), note=body.note
    )
    return character_out(projects.character_row(character))


# --------------------------------------------------------------------------- #
# 四视图
# --------------------------------------------------------------------------- #


def _views_out(result: views.ViewSet, character: Character) -> ViewSetOut:
    return ViewSetOut(
        character_id=result.character_id,
        state=result.state,
        state_label=characters.label(result.state),
        images=[
            ViewImageOut(
                variant=one.variant,
                label=one.label,
                generation_id=one.generation_id,
                file_path=one.file_path,
                width=one.width,
                height=one.height,
                problems=list(one.problems),
                params=one.params,
            )
            for one in result.images
        ],
        failures=[
            ViewFailureOut(variant=one.variant, label=one.label, reason=one.reason)
            for one in result.failures
        ],
        references=list(result.references),
        size_complaint=result.size_complaint,
        ok=result.ok,
    )


@router.post("/{character_id}/views", response_model=ViewSetOut)
def generate_views(
    character_id: str,
    body: ViewsIn,
    project: ProjectDb,
    runtime: RuntimeDb,
    ref: CurrentProject,
) -> ViewSetOut:
    """一次生成一张 2048×2048 的 2×2 四视图四宫格。"""
    if body.variants:
        raise Conflict("四视图现在只能整张生成，不再支持单独重生某个视角")
    character = characters.get(project, character_id)
    result = views.generate_views(
        project,
        runtime,
        ref,
        character,
        seed=body.seed,
    )
    return _views_out(result, character)


@router.get("/{character_id}/views", response_model=list[GenerationOut])
def list_views(character_id: str, project: ProjectDb) -> list[GenerationOut]:
    """四视图候选，`variant=sheet` 是新版单张四宫格，旧分图仍可只读。"""
    characters.get(project, character_id)
    rows = generations.candidates(project, target_ref=character_id, stage=views.STAGE)
    return [_generation_out(row) for row in rows]


@router.post("/{character_id}/views/review", response_model=ViewReviewOut)
def review_views(
    character_id: str,
    body: ViewReviewIn,
    project: ProjectDb,
    runtime: RuntimeDb,
    ref: CurrentProject,
) -> ViewReviewOut:
    """让 `vision_reviewer` 看图裁决。粒度跟项目 `review_mode` 走，`mode` 能盖这一次。

    `regenerate` 默认关：REJECT 后自动重生要花额度，该不该花得用户说了算。裁决只能拦不能
    放行：APPROVE 也不推状态，定稿仍要人来选。
    """
    character = characters.get(project, character_id)
    result = vision.review(
        project,
        runtime,
        ref,
        character,
        mode=body.mode,
        generate=image_gen.generate if body.regenerate else None,
    )
    return ViewReviewOut(
        character_id=result.character_id,
        mode=result.mode,
        decision=result.decision,
        approved=result.approved,
        attempt=result.attempt,
        regenerated=result.regenerated,
        manual=result.manual,
        skipped=result.skipped,
        verdicts=[
            ViewVerdictOut(
                variants=list(one.variants),
                decision=one.decision,
                sections={name: list(items) for name, items in one.verdict.sections.items()},
                text=one.verdict.text,
            )
            for one in result.verdicts
        ],
    )


@router.post("/{character_id}/views/confirm", response_model=CharacterOut)
def confirm_views(
    character_id: str, body: ViewsAdoptIn, project: ProjectDb, ref: CurrentProject
) -> CharacterOut:
    """选择一张完整四宫格定稿；请求仍使用 `picks`，固定键为 `sheet`。"""
    character = characters.get(project, character_id)
    if set(body.picks) != {views.SHEET_CODE}:
        raise Conflict('新四视图只能提交 {"sheet": generation_id} 定稿')
    chosen: dict[str, Generation] = {}
    for code, generation_id in body.picks.items():
        row = generations.get(project, generation_id)
        if row is None:
            raise NotFound(f"产物 {generation_id} 不存在")
        chosen[code] = row
    views.adopt(project, ref, character, chosen, note=body.note)
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
