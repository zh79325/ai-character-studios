"""会话接口：多轮对焦、草稿、确认沉淀，以及项目长期记忆的增删改。

一轮对话是**同步阻塞**的：路由函数用 `def` 而不是 `async def`，FastAPI 会把它放到线程池
里跑，等模型答完再返回。理由是它天然是请求-响应式的——用户按了发送就在等这一轮的结果，
拆成「提交任务 + 轮询状态」只是把复杂度转给前端。生成期间的增量走 SSE 那一路。

流式与结果是两条通道：`POST /messages` 拿最终结果（也是唯一写库的地方），
`GET /stream` 只读进程内的广播缓冲。前端先订流再发消息，断流也不影响这一轮落库。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from fastapi import APIRouter, Header, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sse_starlette import EventSourceResponse, ServerSentEvent

from atelier.agents import conversation as engine
from atelier.agents import parsing
from atelier.agents.stream_bus import BUS, COMMITTED, ERROR, TURN
from atelier.api.deps import CurrentProject, ProjectDb, RuntimeDb
from atelier.api.schemas import (
    ArchivedOut,
    ChoiceGroupOut,
    CommitIn,
    CommitOut,
    ConversationCreateIn,
    ConversationDetailOut,
    ConversationMemoryOut,
    ConversationOut,
    DiffOut,
    DiscardOut,
    DraftOut,
    InterruptOut,
    MessageOut,
    NamingOptionOut,
    ProjectMemoryIn,
    ProjectMemoryOut,
    ProjectMemoryPatch,
    SendMessageIn,
    TurnOut,
)
from atelier.assets import archive, layout
from atelier.assets import memory as memory_files
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import ArtifactDraft, Character, Conversation, Message
from atelier.errors import Conflict, NotFound

router = APIRouter(prefix="/api/projects/{project_code}/conversations", tags=["conversations"])
memory_router = APIRouter(prefix="/api/projects/{project_code}/memory", tags=["memory"])

POLL_SECONDS = 0.15
"""增量的上屏间隔。这条流是给人看字的，比日志面板要快一档。"""

PING_SECONDS = 15

TERMINAL_EVENTS = (TURN, ERROR, COMMITTED)
"""推完这些事件说明这一轮已经有了结果，流可以自己收。"""


# --------------------------------------------------------------------------- #
# 出参装配
# --------------------------------------------------------------------------- #


def _counts(project: Session, conversation_id: str) -> tuple[int, int]:
    messages = project.scalar(
        select(func.count(Message.id)).where(Message.conversation_id == conversation_id)
    )
    drafts = project.scalar(
        select(func.count(ArtifactDraft.id)).where(
            ArtifactDraft.conversation_id == conversation_id,
            ArtifactDraft.status == "pending",
        )
    )
    return int(messages or 0), int(drafts or 0)


def _conversation_out(project: Session, row: Conversation) -> ConversationOut:
    messages, drafts = _counts(project, row.id)
    return ConversationOut(
        id=row.id,
        target_kind=row.target_kind,
        target_ref=row.target_ref,
        agent_code=row.agent_code,
        title=row.title,
        status=row.status,
        bound_provider_label=row.bound_provider_label,
        rebind_count=row.rebind_count,
        rebind_reason=row.rebind_reason,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
        message_count=messages,
        pending_drafts=drafts,
    )


def _message_out(row: Message) -> MessageOut:
    return MessageOut(
        id=row.id,
        turn_no=row.turn_no,
        role=row.role,
        content=row.content,
        token_count=row.token_count,
        folded=row.folded,
        status=row.status,
        agent_code=row.agent_code,
        attachments=row.attachments,
        created_at=row.created_at.isoformat(),
    )


def _is_stale(ref: ProjectRef, draft: ArtifactDraft) -> bool:
    """草稿的基线是否已经过期。

    过期不是错误：用户手改过定稿、或另一个会话先沉淀了，都会让这份草稿失去基线。前端据此
    提前显示「已过期」，而不是等到点了沉淀才收到 409。
    """
    target = layout.resolve_inside(ref.dir, draft.target_path)
    return archive.file_hash(target) != (draft.based_on_hash or "")


def _draft_out(ref: ProjectRef, row: ArtifactDraft) -> DraftOut:
    return DraftOut(
        id=row.id,
        target_path=row.target_path,
        content=row.content,
        based_on_hash=row.based_on_hash,
        status=row.status,
        created_at=row.created_at.isoformat(),
        stale=_is_stale(ref, row),
    )


def _memory_out(entry: memory_files.MemoryEntry, character_ref: str) -> ProjectMemoryOut:
    return ProjectMemoryOut(
        id=entry.id,
        kind=entry.kind,
        content=entry.content,
        character_ref=character_ref,
        enabled=entry.enabled,
    )


def _memory_scopes(
    project: Session, ref: ProjectRef, character_ref: str | None
) -> list[tuple[str, Path]]:
    """要看的那几份偏好文件，项目级在前。返回（作用域, 所在目录）。

    不填 `character_ref` 就把每个角色目录都转一遍：记忆现在按对象分文件存，没有一张表可以
    一次查完。
    """
    scopes: list[tuple[str, Path]] = [("", ref.dir)]
    if character_ref:
        row = project.get(Character, character_ref)
        if row is None:
            raise NotFound(f"角色 {character_ref} 不在这个项目里")
        scopes.append((row.id, ref.absolute(row.dir_name)))
        return scopes
    if character_ref is None:
        rows = project.scalars(select(Character).order_by(Character.created_at)).all()
        scopes.extend((one.id, ref.absolute(one.dir_name)) for one in rows)
    return scopes


def _naming_out(options: Sequence[parsing.NamingOption]) -> list[NamingOptionOut]:
    return [NamingOptionOut(name=item.name, code=item.code, reason=item.reason) for item in options]


def _choices_out(groups: Sequence[parsing.ChoiceGroup]) -> list[ChoiceGroupOut]:
    return [
        ChoiceGroupOut(
            item=one.item,
            options=list(one.options),
            recommended=list(one.recommended),
            multiple=one.multiple,
        )
        for one in groups
    ]


def _detail(project: Session, ref: ProjectRef, row: Conversation) -> ConversationDetailOut:
    memory = engine.agent_memory_of(project, ref, row)
    artifact_path, _ = engine.artifact_of(project, ref, row)
    briefing = engine.briefing_of(project, ref, row)
    return ConversationDetailOut(
        conversation=_conversation_out(project, row),
        messages=[_message_out(m) for m in engine.messages_of(project, row.id)],
        memory=ConversationMemoryOut(
            summary=memory.summary,
            decisions=list(memory.decisions),
            open_questions=list(memory.open_questions),
            folded_turns=memory.folded_turns,
        ),
        drafts=[_draft_out(ref, d) for d in engine.drafts_of(project, row.id)],
        artifact_path=artifact_path,
        naming=_naming_out(engine.naming_of(project, row.id)),
        settled=engine.is_settled(project, row.id),
        choices=_choices_out(engine.choices_of(project, row.id)),
        briefing=briefing.text,
        briefing_blank=briefing.blank,
    )


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[ConversationOut])
def list_conversations(
    project: ProjectDb,
    target_kind: str | None = Query(default=None),
    target_ref: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ConversationOut]:
    """会话列表，最近更新的在前。已沉淀与已丢弃的也留着，历史要能回看。"""
    rows = engine.list_conversations(
        project, target_kind=target_kind, target_ref=target_ref, limit=limit
    )
    return [_conversation_out(project, row) for row in rows]


@router.post("", response_model=ConversationDetailOut, status_code=status.HTTP_201_CREATED)
def create_conversation(
    payload: ConversationCreateIn, project: ProjectDb, ref: CurrentProject
) -> ConversationDetailOut:
    """开一场新会话。第一轮由前端随后发消息触发，这里不预热模型。"""
    row = engine.start(
        project,
        agent_code=payload.agent_code,
        target_kind=payload.target_kind,
        target_ref=payload.target_ref,
        title=payload.title,
    )
    return _detail(project, ref, row)


@router.post("/ensure", response_model=ConversationDetailOut)
def ensure_conversation(
    payload: ConversationCreateIn, project: ProjectDb, ref: CurrentProject
) -> ConversationDetailOut:
    """拿这个对焦对象当下该聊的会话，没有就开一场。

    立项对焦用这一口：那本来就只有一条线，让用户先点一下「新会话」才能说话是白多一步。幂等：
    刷多少次都是同一场，沉淀过也不开新的。因此返的是 200 而不是 201。
    """
    row = engine.ensure(
        project,
        agent_code=payload.agent_code,
        target_kind=payload.target_kind,
        target_ref=payload.target_ref,
        title=payload.title,
    )
    return _detail(project, ref, row)


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
def read_conversation(
    conversation_id: str, project: ProjectDb, ref: CurrentProject
) -> ConversationDetailOut:
    return _detail(project, ref, engine.get(project, conversation_id))


@router.post("/{conversation_id}/messages", response_model=TurnOut)
def send_message(
    conversation_id: str,
    payload: SendMessageIn,
    project: ProjectDb,
    runtime: RuntimeDb,
    ref: CurrentProject,
) -> TurnOut:
    """发一轮并等回答。慢是应该的——用户就在等这一轮的结果。"""
    row = engine.get(project, conversation_id)
    result = engine.send(project, runtime, ref, row, payload.content, stream=payload.stream)
    return TurnOut(
        conversation_id=result.conversation_id,
        turn_no=result.turn_no,
        content=result.content,
        draft_ids=list(result.draft_ids),
        folded_turns=list(result.folded_turns),
        context_tokens=result.context_tokens,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        provider_label=result.provider_label,
        naming=_naming_out(result.naming),
        choices=_choices_out(result.choices),
    )


def _resume_seq(conversation_id: str, after_seq: int, last_event_id: str | None) -> int:
    """续传游标。浏览器自动重连时只会带 `Last-Event-ID`，查询参数是它给不出来的。"""
    if after_seq > 0 or not last_event_id:
        return max(after_seq, 0)
    prefix, _, seq = last_event_id.rpartition(":")
    return int(seq) if prefix == conversation_id and seq.isdigit() else 0


@router.get("/{conversation_id}/stream")
async def stream_conversation(
    request: Request,
    conversation_id: str,
    project: ProjectDb,
    after_seq: int = Query(default=0),
    fresh: bool = Query(default=False),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    """订阅这场会话的生成增量，一轮出了结果就收流。

    不发起任何调用：这条流的作用是「让字一个个出现」，会话的真相始终在库里。

    起点由调用方定。刚发出一轮、要看这一轮的字，就带 `fresh=1`：缓冲里现存的一概不算，只
    等订上来之后新产生的增量。这条流跟 `POST /messages` 几乎同时出发，谁先到服务端说不准，
    赶在开工清缓冲之前从头读就会把上一轮整段重放进「正在想」的气泡，末尾那条 turn 再把流当场
    收掉，这一轮反而一个字也没有。代价是可能漏掉开头几段，比重放错的那一轮划算。

    接一轮别处发起的、已经在跑的生成就不要带：从缓冲头读才能把错过的开头补上。客户端断开或
    进程重启后重连，靠游标续上还在缓冲里的部分，缓冲已滚掉的那些不补——完整回答刷一次消息
    列表就有。

    收流的时机是一轮有了结果（`turn`/`error`/`committed`），不是等客户端断开：这条连接的
    价值只在生成中的那几秒，挂着不收既占着事件循环，也让「什么时候算结束」变成一件没人
    说得清的事。前端下一轮再订一次，或者让 EventSource 自己带着 `Last-Event-ID` 重连。
    """
    engine.get(project, conversation_id)
    cursor = _resume_seq(conversation_id, after_seq, last_event_id)
    if fresh and cursor == 0:
        cursor = BUS.latest_seq(conversation_id)

    async def stream() -> AsyncIterator[ServerSentEvent]:
        nonlocal cursor
        yield ServerSentEvent(event="ready", data=str(cursor))
        while not await request.is_disconnected():
            settled = False
            for item in BUS.since(conversation_id, cursor):
                cursor = item.seq
                yield ServerSentEvent(
                    event=item.event,
                    id=f"{conversation_id}:{item.seq}",
                    data=item.data
                    if isinstance(item.data, str)
                    else json.dumps(item.data, ensure_ascii=False),
                )
                settled = settled or item.event in TERMINAL_EVENTS
            if settled:
                return
            await asyncio.sleep(POLL_SECONDS)

    return EventSourceResponse(stream(), ping=PING_SECONDS)


@router.post("/{conversation_id}/interrupt", response_model=InterruptOut)
def interrupt_conversation(conversation_id: str, project: ProjectDb) -> InterruptOut:
    """中断正在跑的那一轮。

    两件事：库里那条 `thinking` 改成 `cancelled`，紧接着叫停推理。进程重启过、推理早不在了的
    那种卡死也走这一口：叫停落不到人头上，但状态总得能清。
    """
    row = engine.get(project, conversation_id)
    return InterruptOut(conversation_id=conversation_id, interrupted=engine.interrupt(project, row))


@router.post("/{conversation_id}/commit", response_model=CommitOut)
def commit_conversation(
    conversation_id: str,
    payload: CommitIn,
    project: ProjectDb,
    ref: CurrentProject,
) -> CommitOut:
    """确认沉淀：这是整条链路里唯一会改工作区文件的接口。沉淀完会话继续开着，可以接着聊。"""
    row = engine.get(project, conversation_id)
    result = engine.commit(project, ref, row, draft_ids=payload.draft_ids)
    return CommitOut(
        conversation_id=result.conversation_id,
        archived=[
            ArchivedOut(
                target_path=item.target_path,
                content_hash=item.content_hash,
                previous_path=item.previous_path,
            )
            for item in result.archived
        ],
        memories_added=list(result.memories_added),
    )


@router.post("/{conversation_id}/discard", response_model=DiscardOut)
def discard_conversation(conversation_id: str, project: ProjectDb) -> DiscardOut:
    """丢弃草稿。会话与消息全留着，也还能接着聊，只是这批草稿不往磁盘上落了。"""
    row = engine.get(project, conversation_id)
    count = engine.discard(project, row)
    BUS.drop(conversation_id)
    return DiscardOut(conversation_id=conversation_id, discarded=count)


@router.get("/{conversation_id}/drafts/{draft_id}/diff", response_model=DiffOut)
def read_diff(
    conversation_id: str, draft_id: str, project: ProjectDb, ref: CurrentProject
) -> DiffOut:
    """草稿与当前定稿的两份全文，diff 由前端渲染。

    后端不算 diff：算法与展示形式（并排、行内、折叠未变区）都是前端的事，把结果算成文本
    传过去反而限制了它。
    """
    draft = project.get(ArtifactDraft, draft_id)
    if draft is None or draft.conversation_id != conversation_id:
        raise NotFound(f"草稿 {draft_id} 不在会话 {conversation_id} 里")
    target = layout.resolve_inside(ref.dir, draft.target_path)
    current = target.read_text(encoding="utf-8") if target.is_file() else ""
    return DiffOut(
        target_path=draft.target_path,
        current=current,
        draft=draft.content,
        stale=_is_stale(ref, draft),
        warnings=engine.draft_warnings(ref, draft.target_path, draft.content),
    )


# --------------------------------------------------------------------------- #
# 项目长期记忆
# --------------------------------------------------------------------------- #


@memory_router.get("", response_model=list[ProjectMemoryOut])
def list_memories(
    project: ProjectDb,
    ref: CurrentProject,
    character_ref: str | None = Query(
        default=None, description="填角色 id 只看它那一档加项目级；不填看全部"
    ),
) -> list[ProjectMemoryOut]:
    """项目记忆全量，含已停用的——停用是让它不再注入，不是删掉。

    不填 `character_ref` 给全部：设置页要能一眼看完这个项目攒下的所有记忆，包括各个角色名下
    那些——拿不到全量就无法解释模型为何还带着某条旧偏好。
    """
    out: list[ProjectMemoryOut] = []
    for scope, base_dir in _memory_scopes(project, ref, character_ref):
        out.extend(_memory_out(entry, scope) for entry in memory_files.read_preferences(base_dir))
    return out


@memory_router.post("", response_model=ProjectMemoryOut, status_code=status.HTTP_201_CREATED)
def add_memory(
    payload: ProjectMemoryIn, project: ProjectDb, ref: CurrentProject
) -> ProjectMemoryOut:
    """手写一条记忆。与 Agent 沉淀出来的进同一份文件、走同一套去重。"""
    added = engine.write_memory(
        project, ref, payload.kind, payload.content, character_ref=payload.character_ref
    )
    if added is None:
        raise Conflict("这条记忆已经有了")
    return _memory_out(added, payload.character_ref)


@memory_router.patch("/{memory_id}", response_model=ProjectMemoryOut)
def update_memory(
    memory_id: str, payload: ProjectMemoryPatch, project: ProjectDb, ref: CurrentProject
) -> ProjectMemoryOut:
    """改一条。改完 `id` 会变：条目按内容哈希寻址，前端拿响应里的新 id 重拉列表。"""
    for scope, base_dir in _memory_scopes(project, ref, None):
        updated = memory_files.update_preference(
            base_dir,
            memory_id,
            scope=memory_files.SCOPE_CHARACTER if scope else memory_files.SCOPE_PROJECT,
            content=payload.content,
            enabled=payload.enabled,
        )
        if updated is not None:
            return _memory_out(updated, scope)
    raise NotFound(f"记忆 {memory_id} 不存在")


@memory_router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(memory_id: str, project: ProjectDb, ref: CurrentProject) -> None:
    for scope, base_dir in _memory_scopes(project, ref, None):
        if memory_files.delete_preference(
            base_dir,
            memory_id,
            scope=memory_files.SCOPE_CHARACTER if scope else memory_files.SCOPE_PROJECT,
        ):
            return
    raise NotFound(f"记忆 {memory_id} 不存在")
