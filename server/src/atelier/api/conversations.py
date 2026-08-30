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
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sse_starlette import EventSourceResponse, ServerSentEvent

from atelier.agents import conversation as engine
from atelier.agents.stream_bus import BUS, COMMITTED, ERROR, TURN
from atelier.api.deps import CurrentProject, ProjectDb, RuntimeDb
from atelier.api.schemas import (
    ArchivedOut,
    CommitIn,
    CommitOut,
    ConversationCreateIn,
    ConversationDetailOut,
    ConversationMemoryOut,
    ConversationOut,
    DiffOut,
    DiscardOut,
    DraftOut,
    MessageOut,
    ProjectMemoryIn,
    ProjectMemoryOut,
    ProjectMemoryPatch,
    SendMessageIn,
    TurnOut,
)
from atelier.assets import archive, layout
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import ArtifactDraft, Conversation, Message, ProjectMemory
from atelier.errors import Conflict, NotFound

router = APIRouter(prefix="/api/conversations", tags=["conversations"])
memory_router = APIRouter(prefix="/api/memory", tags=["memory"])

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


def _memory_out(row: ProjectMemory) -> ProjectMemoryOut:
    return ProjectMemoryOut(
        id=row.id,
        kind=row.kind,
        content=row.content,
        character_ref=row.character_ref,
        enabled=row.enabled,
        source_conversation_id=row.source_conversation_id,
        created_at=row.created_at.isoformat(),
    )


def _detail(project: Session, ref: ProjectRef, row: Conversation) -> ConversationDetailOut:
    memory = engine.memory_of(project, row.id)
    artifact_path, _ = engine.artifact_of(project, ref, row)
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
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    """订阅这场会话的生成增量，一轮出了结果就收流。

    只读广播缓冲，不查库也不发起调用：这条流的作用是「让字一个个出现」，会话的真相始终
    在库里。客户端断开或进程重启后重连，靠游标续上还在缓冲里的部分，缓冲已滚掉的那些不
    补——完整回答刷一次消息列表就有。

    收流的时机是一轮有了结果（`turn`/`error`/`committed`），不是等客户端断开：这条连接的
    价值只在生成中的那几秒，挂着不收既占着事件循环，也让「什么时候算结束」变成一件没人
    说得清的事。前端下一轮再订一次，或者让 EventSource 自己带着 `Last-Event-ID` 重连。
    """
    engine.get(project, conversation_id)
    cursor = _resume_seq(conversation_id, after_seq, last_event_id)

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


@router.post("/{conversation_id}/commit", response_model=CommitOut)
def commit_conversation(
    conversation_id: str,
    payload: CommitIn,
    project: ProjectDb,
    ref: CurrentProject,
) -> CommitOut:
    """确认沉淀：这是整条链路里唯一会改工作区文件的接口。"""
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
    """丢弃草稿。会话与消息全留着，只是不再往磁盘上落。"""
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
    character_ref: str | None = Query(
        default=None, description="填角色 id 只看它那一档加项目级；不填看全部"
    ),
) -> list[ProjectMemoryOut]:
    """项目记忆全量，含已停用的——停用是让它不再注入，不是删掉。

    不填 `character_ref` 给全部：设置页要能一眼看完这个项目攒下的所有记忆，包括各个角色名下
    那些——拿不到全量就无法解释模型为何还带着某条旧偏好。
    """
    stmt = select(ProjectMemory).order_by(ProjectMemory.created_at)
    if character_ref is not None:
        stmt = stmt.where(ProjectMemory.character_ref.in_(("", character_ref)))
    rows = project.scalars(stmt).all()
    return [_memory_out(row) for row in rows]


@memory_router.post("", response_model=ProjectMemoryOut, status_code=status.HTTP_201_CREATED)
def add_memory(payload: ProjectMemoryIn, project: ProjectDb) -> ProjectMemoryOut:
    """手写一条记忆。与 Agent 沉淀出来的走同一张表、同一套去重。"""
    added = engine.write_memory(
        project, payload.kind, payload.content, character_ref=payload.character_ref
    )
    if added is None:
        raise Conflict("这条记忆已经有了")
    project.commit()
    return _memory_out(added)


@memory_router.patch("/{memory_id}", response_model=ProjectMemoryOut)
def update_memory(
    memory_id: str, payload: ProjectMemoryPatch, project: ProjectDb
) -> ProjectMemoryOut:
    row = project.get(ProjectMemory, memory_id)
    if row is None:
        raise NotFound(f"记忆 {memory_id} 不存在")
    if payload.content is not None:
        row.content = payload.content
        row.content_hash = engine.memory_hash(row.kind, payload.content)
    if payload.enabled is not None:
        row.enabled = payload.enabled
    project.commit()
    return _memory_out(row)


@memory_router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(memory_id: str, project: ProjectDb) -> None:
    row = project.get(ProjectMemory, memory_id)
    if row is None:
        raise NotFound(f"记忆 {memory_id} 不存在")
    project.delete(row)
    project.commit()
