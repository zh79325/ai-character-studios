"""会话引擎：一轮对话从收到用户输入到落库的完整过程。

职责边界：

- 上下文怎么拼由 `context` 决定，本模块只负责喂给它库里的东西
- 用哪个候选由 `providers.router` 决定，本模块只把会话行当粘性绑定传进去
- 落盘由 `assets.archive` 决定，本模块只在用户确认时调它

两条不能松的规则：

1. **未确认不落盘**。产物只进 `artifact_drafts`；确认沉淀是唯一的写工作区入口。
2. **原文只折不删**。超预算时把最老的消息压缩进摘要并标 `folded=true`，`messages` 里
   的原文永远留着，前端展开还能看到当时到底说了什么。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from atelier.agents import context, dispatch, parsing, tokens
from atelier.agents.definitions import AgentDefinition, get_agent
from atelier.agents.stream_bus import BUS, COMMITTED, DELTA, ERROR, TURN
from atelier.assets import archive, characters, layout, projects
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import (
    ArtifactDraft,
    Character,
    Conversation,
    ConversationMemory,
    Message,
    ProjectAgentPrompt,
    ProjectMemory,
)
from atelier.db.task_events import record as record_event
from atelier.errors import Conflict, NotFound
from atelier.providers import router, text_chat
from atelier.providers.base import Decision, ProviderError
from atelier.settings import get_settings

_log = structlog.get_logger(__name__)

TARGET_KINDS = ("project", "character")

MAX_FOLD_ROUNDS = 5
"""一轮里最多折几次。

正常一两次就够；卡在这个上限说明单条消息本身就超预算（用户粘了一整篇文档），继续折也
压不下来，此时宁可带着超预算发出去让供应商报错，也比无声地空转几十次好。
"""

FOLD_SUMMARY_SYSTEM = "你在为「{role}」这场对话做前情摘要，只做压缩，不要参与讨论。"

_WHITESPACE_RE = re.compile(r"\s+")

ChatFn = dispatch.ChatFn
"""对话调用口。测试与离线冒烟用假实现替换，签名跟 `text_chat.complete` 一致。

不拿 `text_chat.complete` 当默认值而是进函数再取：默认值在定义时就绑死了，那样把模块属性
换成假实现也拦不住这条路——一不小心就在单测里真的发出去了。
"""


# --------------------------------------------------------------------------- #
# 结果类型
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TurnResult:
    """一轮的结果。前端据此刷消息列表与草稿面板。"""

    conversation_id: str
    turn_no: int
    content: str
    draft_ids: tuple[str, ...] = ()
    folded_turns: tuple[int, ...] = ()
    context_tokens: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    provider_label: str = ""
    naming: tuple[parsing.NamingOption, ...] = ()
    """这轮给的项目命名建议，只在立项对焦里会有。"""


@dataclass(frozen=True, slots=True)
class CommitResult:
    """一次确认沉淀的结果。"""

    conversation_id: str
    archived: tuple[archive.ArchiveResult, ...] = ()
    memories_added: tuple[str, ...] = ()
    """新写进 project_memory 的内容，去重后剩下的那些。"""


@dataclass(slots=True)
class ContextInputs:
    """组装一轮上下文所需的库内材料。"""

    agent: AgentDefinition
    addendum: str | None = None
    artifact_path: str | None = None
    artifact_text: str | None = None
    config_path: str | None = None
    config_text: str | None = None
    project_memories: list[ProjectMemory] = field(default_factory=list)
    memory: ConversationMemory | None = None
    messages: list[Message] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #


def start(
    project: Session,
    *,
    agent_code: str,
    target_kind: str,
    target_ref: str | None = None,
    title: str = "",
) -> Conversation:
    """开一个新会话。

    不复用旧会话：每次对焦都是一段独立的记录，摘要与结论也只在这段里滚。要在旧结论上继续
    改，靠的是「新会话先把当前定稿读进上下文」，而不是把两次对话接成一条越来越长的流。
    """
    agent = get_agent(agent_code)
    if not agent.conversational:
        raise Conflict(f"{agent_code} 不是会话型 Agent，不能开会话")
    if target_kind not in TARGET_KINDS:
        raise Conflict(f"target_kind 只能是 {TARGET_KINDS} 之一")
    if target_kind == "character" and not target_ref:
        raise Conflict("角色会话必须指明是哪个角色")

    conversation = Conversation(
        id=uuid.uuid4().hex,
        target_kind=target_kind,
        target_ref=target_ref,
        agent_code=agent_code,
        title=title.strip()[:255],
        status="active",
    )
    project.add(conversation)
    project.add(ConversationMemory(conversation_id=conversation.id))
    project.commit()
    _log.info("conversation_started", id=conversation.id, agent=agent_code, target=target_ref)
    return conversation


def get(project: Session, conversation_id: str) -> Conversation:
    conversation = project.get(Conversation, conversation_id)
    if conversation is None:
        raise NotFound(f"会话 {conversation_id} 不存在")
    return conversation


def ensure(
    project: Session,
    *,
    agent_code: str,
    target_kind: str,
    target_ref: str | None = None,
    title: str = "",
) -> Conversation:
    """这个对焦对象当下该聊的那场会话，没有就开一场。

    接着最近那场还开着的聊；上一场已经沉淀或丢弃了才开新的——冻结的会话发不出消息，把它
    摆上来等于让用户自己去发现「原来得先开一场新的」。
    """
    for row in list_conversations(project, target_kind=target_kind, target_ref=target_ref):
        if row.agent_code == agent_code and row.status == "active":
            return row
    return start(
        project,
        agent_code=agent_code,
        target_kind=target_kind,
        target_ref=target_ref,
        title=title,
    )


def list_conversations(
    project: Session,
    *,
    target_kind: str | None = None,
    target_ref: str | None = None,
    limit: int = 50,
) -> list[Conversation]:
    stmt = select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
    if target_kind is not None:
        stmt = stmt.where(Conversation.target_kind == target_kind)
    if target_ref is not None:
        stmt = stmt.where(Conversation.target_ref == target_ref)
    return list(project.scalars(stmt))


def messages_of(project: Session, conversation_id: str) -> list[Message]:
    return list(
        project.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.turn_no)
        )
    )


def memory_of(project: Session, conversation_id: str) -> ConversationMemory:
    """会话记忆行，缺了就补一条——老会话可能是在这张表之前建的。"""
    memory = project.get(ConversationMemory, conversation_id)
    if memory is None:
        memory = ConversationMemory(conversation_id=conversation_id)
        project.add(memory)
        project.flush()
    return memory


def naming_of(project: Session, conversation_id: str) -> tuple[parsing.NamingOption, ...]:
    """这场会话目前的命名建议：从最近一条带过该块的助手消息里现场解析。

    不建表也不加列：建议本身就存在消息原文里，再存一份就多一处要对账的状态。只认最近那
    一条：聊到后面模型会改主意，把历史建议堆在一起只会让用户面对十几个过期选项。
    """
    rows = project.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.turn_no.desc())
    )
    for row in rows:
        options = parsing.parse_naming(row.content)
        if options:
            return options
    return ()


def drafts_of(
    project: Session, conversation_id: str, *, status: str | None = "pending"
) -> list[ArtifactDraft]:
    stmt = select(ArtifactDraft).where(ArtifactDraft.conversation_id == conversation_id)
    if status is not None:
        stmt = stmt.where(ArtifactDraft.status == status)
    return list(project.scalars(stmt.order_by(ArtifactDraft.created_at)))


PROJECT_SCOPE = ""
"""项目级记忆的 `character_ref`。空串而不是 None，理由在 `ProjectMemory` 里。"""


def memory_scope(conversation: Conversation) -> str:
    """这场会话里聊出来的记忆归谁。

    看的是对焦对象，不是 Agent 声明的 `memory_scope`：同一个 `spec_writer` 在不同角色上聊出
    的偏好本来就不能混在一起，而声明只能说清它该**看到**哪一档。
    """
    if conversation.target_kind == "character" and conversation.target_ref:
        return conversation.target_ref
    return PROJECT_SCOPE


def enabled_memories(project: Session, character_ref: str = PROJECT_SCOPE) -> list[ProjectMemory]:
    """该注入的记忆：项目级的总带上，再加当前角色自己那些。

    别的角色那几条不带：「赤瞳的尾巴要 2 条」对下一个角色不仅无用，还会被模型当成本项目的
    通例写进新设定，用户得花一轮把它推翻。
    """
    scopes = {PROJECT_SCOPE, character_ref}
    return list(
        project.scalars(
            select(ProjectMemory)
            .where(
                ProjectMemory.enabled.is_(True),
                ProjectMemory.character_ref.in_(sorted(scopes)),
            )
            .order_by(ProjectMemory.created_at)
        )
    )


# --------------------------------------------------------------------------- #
# 目标定稿
# --------------------------------------------------------------------------- #


def _character(project: Session, target_ref: str | None) -> Character:
    character = project.get(Character, target_ref) if target_ref else None
    if character is None:
        raise NotFound(f"角色 {target_ref} 不在这个项目里")
    return character


def artifact_of(
    project: Session, ref: ProjectRef, conversation: Conversation
) -> tuple[str | None, str | None]:
    """这场会话在改哪个定稿文件，以及它现在的内容。

    返回相对路径而不是绝对路径：路径会进上下文给模型看，绝对路径既没用又把用户的目录结构
    带进了 prompt。文件还不存在时路径照样返回——模型要知道自己该往哪儿写。
    """
    if conversation.target_kind == "project":
        config = projects.read_config(ref.dir)
        path = layout.art_bible_path(ref.dir, config.art_bible)
        return config.art_bible, path.read_text(encoding="utf-8") if path.is_file() else ""

    character = _character(project, conversation.target_ref)
    relative = characters.spec_target(character)
    target = layout.resolve_inside(ref.dir, relative)
    return relative, target.read_text(encoding="utf-8") if target.is_file() else ""


BRIEFING_BLANK = "说出你的想法，让我们开始这个伟大的项目吧"
"""白纸项目就一句号召。写长了反而像说明书，而这一屏要的只是让用户把第一句话敲出来。"""

BRIEFING_HEAD = "这个项目现在手里有这些："

BRIEFING_TAIL_DRAFTING = (
    "要接着对焦就直接说改哪儿；定得差不多了就选一组名字与代号，点「完成立项」。"
)

BRIEFING_TAIL_READY = "要改哪儿直接说；没什么要改就可以去做素材了。"


@dataclass(frozen=True, slots=True)
class Briefing:
    """对焦面板的开场提示。

    `blank` 是给前端分形态用的：白纸项目只有一句号召，摆成气泡像是 AI 已经发过话了，前端把
    它铺成居中的大字；有现状可报时才是一段正常的开场发言。
    """

    text: str = ""
    blank: bool = False


def briefing_of(project: Session, ref: ProjectRef, conversation: Conversation) -> Briefing:
    """对焦面板最前面那段话：项目现在是什么样、接下来该说什么。

    平台自己拼，不花模型调用：这段话每次打开项目都要显示，而内容全是磁盘与库里现成的事实。
    也不入库：存下来就会跟不上后续沉淀，用户下次进来看到的就是一份过期总结。

    只给项目会话：角色会话的现状就是那份设定稿本身，它已经摆在旁边的面板里了。
    """
    if conversation.target_kind != "project":
        return Briefing()
    config = projects.read_config(ref.dir)
    path = projects.art_bible_path(ref, config)
    art_bible = path.read_text(encoding="utf-8") if path.is_file() else ""
    facts = _project_facts(project, config, art_bible)
    if not facts:
        return Briefing(text=BRIEFING_BLANK, blank=True)
    tail = BRIEFING_TAIL_DRAFTING if config.stage == "drafting" else BRIEFING_TAIL_READY
    body = "\n".join(f"- {one}" for one in facts)
    return Briefing(text=f"{BRIEFING_HEAD}\n{body}\n\n{tail}")


def _project_facts(project: Session, config: projects.ProjectConfig, art_bible: str) -> list[str]:
    """项目已经有的东西，一条一句人话。空列表就是「还没开始」。

    立项期不报名字与代号：那时候名字只是目录名、代号是临时的，拿它们当「已有的东西」报
    给用户只会让他以为这两个已经定下了。
    """
    facts: list[str] = []
    if config.stage != "drafting":
        facts.append(f"名字与代号：{config.name}（{config.code}）")

    style = [
        value.strip()
        for value in (config.style.art_style, config.style.mood, config.style.palette)
        if value.strip()
    ]
    if style:
        facts.append("风格基调：" + "、".join(style))

    if art_bible.strip():
        gaps = projects.art_bible_gaps(art_bible)
        done = max(len(projects.ART_BIBLE_SECTIONS) - len(gaps), 0)
        rest = "，还差：" + "、".join(gaps) if gaps else "，六节都齐了"
        facts.append(f"视觉规范 {config.art_bible}：六节里写好了 {done} 节{rest}")

    count = int(project.scalar(select(func.count(Character.id))) or 0)
    if count:
        facts.append(f"已经有 {count} 个角色建在项目里")
    return facts


def config_snapshot(ref: ProjectRef) -> str:
    """`project.json` 里归 Agent 改的那几段，原样序列化给它看。

    只给这几个键，不给整份：`code` 与 `art_bible` 是平台的账，摆进上下文只会让 Agent 觉得
    自己也能改。给现值而不是给一句「保持原样」，是因为改设定的会话要说清「哪一处改了」，
    没有现值它只能整份重写，那正是这套流程要避免的事。
    """
    config = projects.read_config(ref.dir)
    dumped = config.model_dump(mode="json")
    kept = {key: dumped[key] for key in archive.MERGED_CONFIG_KEYS if key in dumped}
    return json.dumps(kept, ensure_ascii=False, indent=2)


def allowed_draft_targets(
    project: Session, ref: ProjectRef, conversation: Conversation
) -> tuple[str, ...]:
    """这场会话准许写哪些定稿位。

    立项会话只碰项目根上那两份；角色会话只碰自己那个角色目录。不设这道栅栏的话，`spec_writer`
    声明一句 `[草稿开始: art-bible.md]` 就能把整个项目的视觉真相顶掉——它自己未必是故意的，
    但用户在草稿面板上看到的只是「一份 art-bible.md 的改动」，很容易顺手确认。
    """
    if conversation.target_kind == "project":
        config = projects.read_config(ref.dir)
        return (config.art_bible, layout.PROJECT_JSON)
    character = _character(project, conversation.target_ref)
    return (f"{character.dir_name}/",)


def _check_allowed(
    project: Session, ref: ProjectRef, conversation: Conversation, relative: str
) -> None:
    allowed = allowed_draft_targets(project, ref, conversation)
    if any(relative == one or (one.endswith("/") and relative.startswith(one)) for one in allowed):
        return
    raise Conflict(f"这场会话只能写 {'、'.join(allowed)}，{relative} 不在它的职责范围内")


def resolve_draft_path(
    project: Session, ref: ProjectRef, conversation: Conversation, raw: str
) -> str:
    """把 Agent 声明的落盘位置校成一个项目内的相对路径。

    Agent 只会写文件名（提示词里就是 `{角色名}角色设定.md`），角色会话下要把它归到该角色
    的目录里去，否则一堆设定文档全落在项目根上。越界的路径直接由 `resolve_inside` 拦住：
    模型写出 `../../.ssh/config` 不该有任何机会落地。路径合法之后还要过一道职责白名单——
    在项目目录里不等于是这场会话该改的东西。
    """
    candidate = raw.strip().replace("\\", "/").lstrip("/")
    if not candidate:
        raise Conflict("草稿没有声明落盘位置")

    if conversation.target_kind == "character" and "/" not in candidate:
        character = _character(project, conversation.target_ref)
        candidate = f"{character.dir_name}/{candidate}"

    target = layout.resolve_inside(ref.dir, candidate)
    if target == ref.dir.resolve():
        raise Conflict("草稿的落盘位置不能是项目目录本身")
    relative = ref.relative(target)
    _check_allowed(project, ref, conversation, relative)
    return relative


# --------------------------------------------------------------------------- #
# 一轮对话
# --------------------------------------------------------------------------- #


def addendum(project: Session, agent_code: str) -> str | None:
    """项目级附加指令。单次调用型 Agent 也要带上——不带就成了评审按的标准跟创作按的标准不一样。"""
    row = project.scalars(
        select(ProjectAgentPrompt).where(
            ProjectAgentPrompt.agent_code == agent_code,
            ProjectAgentPrompt.enabled.is_(True),
        )
    ).one_or_none()
    return row.content if row is not None else None


def _inputs(project: Session, ref: ProjectRef, conversation: Conversation) -> ContextInputs:
    agent = get_agent(conversation.agent_code)
    artifact_path, artifact_text = artifact_of(project, ref, conversation)
    # 只有立项会话改得动 project.json，角色会话看见它也用不上，白占预算
    is_project = conversation.target_kind == "project"
    return ContextInputs(
        agent=agent,
        addendum=addendum(project, conversation.agent_code),
        artifact_path=artifact_path,
        artifact_text=artifact_text,
        config_path=layout.PROJECT_JSON if is_project else None,
        config_text=config_snapshot(ref) if is_project else None,
        project_memories=enabled_memories(project, memory_scope(conversation)),
        memory=memory_of(project, conversation.id),
        messages=messages_of(project, conversation.id),
    )


def _assemble(inputs: ContextInputs) -> context.Assembled:
    settings = get_settings()
    return context.assemble(
        inputs.agent,
        inputs.messages,
        addendum=inputs.addendum,
        artifact_path=inputs.artifact_path,
        artifact_text=inputs.artifact_text,
        config_path=inputs.config_path,
        config_text=inputs.config_text,
        project_memories=inputs.project_memories,
        memory=inputs.memory,
        recent_turns=settings.recent_turns,
    )


def _next_turn_no(project: Session, conversation_id: str) -> int:
    current = project.scalar(
        select(func.max(Message.turn_no)).where(Message.conversation_id == conversation_id)
    )
    return int(current or 0) + 1


def _add_message(
    project: Session, conversation: Conversation, role: str, content: str, token_count: int
) -> Message:
    message = Message(
        conversation_id=conversation.id,
        turn_no=_next_turn_no(project, conversation.id),
        role=role,
        content=content,
        token_count=token_count,
    )
    project.add(message)
    project.flush()
    return message


def send(
    project: Session,
    runtime: Session,
    ref: ProjectRef,
    conversation: Conversation,
    text: str,
    *,
    chat: ChatFn | None = None,
    stream: bool = True,
) -> TurnResult:
    """走完一轮：记录用户输入 → 按需折叠 → 调模型 → 记录回答与草稿。"""
    caller: ChatFn = chat if chat is not None else text_chat.complete
    if conversation.status != "active":
        ended = "沉淀" if conversation.status == "committed" else "丢弃"
        raise Conflict(f"会话已{ended}，要继续改就开一个新会话")
    body = text.strip()
    if not body:
        raise Conflict("发给 Agent 的内容不能为空")

    agent = get_agent(conversation.agent_code)
    if not agent.conversational:
        raise Conflict(f"{agent.agent_code} 不是会话型 Agent")

    _add_message(project, conversation, "user", body, tokens.estimate_text(body))
    project.commit()

    inputs = _inputs(project, ref, conversation)
    try:
        decision = _select(runtime, project, ref, conversation, agent)

        folded = _fold_until_fits(project, runtime, ref, conversation, inputs, decision, caller)
        assembled = _assemble(inputs)

        on_delta = _delta_publisher(conversation.id) if stream else None
        reply = _call(
            runtime, project, ref, conversation, agent, decision, assembled, caller, on_delta
        )

        content = reply.content.strip()
        if not content:
            raise ProviderError(f"{decision.candidate.label} 返回了空回答")
    except Exception as exc:
        # 炸了也要说一声：订流的那头只认这条广播，不发它前端就一直等着字出现
        BUS.publish(conversation.id, ERROR, str(exc))
        raise

    assistant = _add_message(
        project,
        conversation,
        "assistant",
        content,
        reply.completion_tokens or tokens.estimate_text(content),
    )
    parsed = parsing.parse_turn(content)
    _apply_progress(inputs.memory, parsed.progress)
    draft_ids = _store_drafts(project, ref, conversation, parsed.drafts)
    conversation.updated_at = assistant.created_at
    project.commit()

    result = TurnResult(
        conversation_id=conversation.id,
        turn_no=assistant.turn_no,
        content=content,
        draft_ids=draft_ids,
        folded_turns=folded,
        context_tokens=assembled.tokens,
        prompt_tokens=reply.prompt_tokens,
        completion_tokens=reply.completion_tokens,
        provider_label=decision.candidate.label,
        naming=parsed.naming,
    )
    BUS.publish(conversation.id, TURN, {"turn_no": assistant.turn_no, "drafts": list(draft_ids)})
    return result


def _delta_publisher(conversation_id: str) -> Callable[[str], None]:
    def publish(piece: str) -> None:
        BUS.publish(conversation_id, DELTA, piece)

    return publish


def _select(
    runtime: Session,
    project: Session,
    ref: ProjectRef,
    conversation: Conversation,
    agent: AgentDefinition,
) -> Decision:
    """选候选并把绑定的变化落进项目库。

    路由层只在传进去的会话对象上改字段，提交是这边的事——它手里的 Session 是全局库，
    提交它落不了项目库里的会话行。
    """
    decision = router.select_candidate(
        runtime,
        agent.agent_code,
        binding=conversation,
        limit_kind="tokens",
        project_code=ref.code,
    )
    project.commit()
    return decision


def _call(
    runtime: Session,
    project: Session,
    ref: ProjectRef,
    conversation: Conversation,
    agent: AgentDefinition,
    decision: Decision,
    assembled: context.Assembled,
    chat: ChatFn,
    on_delta: Callable[[str], None] | None,
) -> text_chat.ChatReply:
    """发出去并记账。重试与换候选的规矩在 `dispatch`，这里只多一件会话自己的事：换了候选要
    把新的绑定与原因落进会话行，下一轮才知道该接着粘在谁身上。
    """

    def rebind(error: ProviderError) -> Decision:
        picked = _select(runtime, project, ref, conversation, agent)
        conversation.rebind_reason = str(error)[:255]
        project.commit()
        return picked

    return dispatch.call(
        runtime,
        agent.agent_code,
        decision,
        assembled.payload(),
        chat,
        project_code=ref.code,
        on_delta=on_delta,
        reselect=rebind,
    )


# --------------------------------------------------------------------------- #
# 折叠与摘要
# --------------------------------------------------------------------------- #


def _fold_until_fits(
    project: Session,
    runtime: Session,
    ref: ProjectRef,
    conversation: Conversation,
    inputs: ContextInputs,
    decision: Decision,
    chat: ChatFn,
) -> tuple[int, ...]:
    """超预算就把最老的原文压缩进摘要，直到装得下或折不动为止。"""
    memory = memory_of(project, conversation.id)
    recent_turns = get_settings().recent_turns
    folded: list[int] = []

    for _ in range(MAX_FOLD_ROUNDS):
        assembled = _assemble(inputs)
        # 窗口外的一定要折（不折就真丢了），超预算那批跟它们合成一次压缩，省一轮调用
        plan = set(context.overflow_turns(inputs.messages, recent_turns))
        plan.update(context.fold_plan(assembled))
        if not plan:
            break

        victims = [m for m in inputs.messages if m.turn_no in plan and not m.folded]
        if not victims:
            break

        request = context.fold_request(victims, memory.summary)
        reply = chat(
            decision.candidate,
            [
                {
                    "role": "system",
                    "content": FOLD_SUMMARY_SYSTEM.format(role=inputs.agent.role),
                },
                {"role": "user", "content": request},
            ],
        )
        summary = reply.content.strip()
        if not summary:
            _log.warning("fold_empty_summary", conversation=conversation.id)
            break

        memory.summary = summary
        memory.folded_turns += len(victims)
        for message in victims:
            message.folded = True
        folded.extend(m.turn_no for m in victims)
        router.report_success(
            runtime,
            inputs.agent.agent_code,
            decision,
            dispatch.outcome_of(reply),
            project_code=ref.code,
        )
        project.commit()
        _log.info(
            "conversation_folded",
            conversation=conversation.id,
            turns=[m.turn_no for m in victims],
            folded_total=memory.folded_turns,
        )

    inputs.memory = memory
    return tuple(folded)


def _merge_unique(existing: Sequence[str], incoming: Sequence[str]) -> list[str]:
    """按出现顺序合并去重，已有的不动。"""
    merged = list(existing)
    seen = {_normalize(item) for item in merged}
    for item in incoming:
        key = _normalize(item)
        if key and key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


def _apply_progress(memory: ConversationMemory | None, progress: parsing.Progress | None) -> None:
    """把这轮的进度并进会话记忆。

    结论累加、开放问题整体替换：结论一旦拍板就不该因为某轮忘了复述而消失，而开放问题的
    最新一份才是准的——上一轮问完的问题这轮不该还挂着。
    """
    if memory is None or progress is None:
        return
    if progress.decisions:
        memory.decisions = _merge_unique(memory.decisions, progress.decisions)
    if progress.open_questions:
        memory.open_questions = list(progress.open_questions)


# --------------------------------------------------------------------------- #
# 草稿
# --------------------------------------------------------------------------- #


def draft_warnings(ref: ProjectRef, target_path: str, content: str) -> list[str]:
    """这份草稿沉下去之前该让用户知道的事。

    不拦沉淀，只摆到台面上：写一半先存下来、回头接着聊是正当的用法，但 art bible 里没填的
    那几节、project.json 里不会生效的那几键，得在按确认之前就看得见。
    """
    if target_path.rsplit("/", 1)[-1] == layout.PROJECT_JSON:
        return archive.config_patch_warnings(ref, content)
    if target_path == projects.read_config(ref.dir).art_bible:
        return projects.art_bible_gaps(content)
    return []


def _store_drafts(
    project: Session,
    ref: ProjectRef,
    conversation: Conversation,
    drafts: Sequence[parsing.DraftBlock],
) -> tuple[str, ...]:
    """草稿入库，同一目标的旧草稿标为过期。

    只留最新一份 pending：一场对话里 Agent 会反复重出 art-bible 全文，留着历史份只会让
    「确认沉淀」面对一堆同名草稿不知道该写哪个。历史版本要看的是消息原文。
    """
    created: list[str] = []
    for block in drafts:
        target_path = resolve_draft_path(project, ref, conversation, block.target_path)
        for stale in drafts_of(project, conversation.id):
            if stale.target_path == target_path:
                stale.status = "superseded"

        absolute = layout.resolve_inside(ref.dir, target_path)
        draft = ArtifactDraft(
            id=uuid.uuid4().hex,
            conversation_id=conversation.id,
            target_path=target_path,
            content=block.content,
            based_on_hash=archive.file_hash(absolute),
            status="pending",
        )
        project.add(draft)
        project.flush()
        created.append(draft.id)
        _log.info("draft_stored", conversation=conversation.id, target=target_path)
    return tuple(created)


# --------------------------------------------------------------------------- #
# 确认沉淀 / 丢弃
# --------------------------------------------------------------------------- #


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def memory_hash(kind: str, content: str) -> str:
    """记忆去重的键。按「类别 + 归一化内容」算，空白差异不算两条。"""
    return hashlib.sha256(f"{kind}:{_normalize(content)}".encode()).hexdigest()


def harvest_memories(project: Session, conversation_id: str) -> tuple[parsing.MemoryItem, ...]:
    """从会话的助手消息里收集 `[项目记忆]` 条目，新的在前。

    在沉淀时才收而不是每轮就写：聊到一半的偏好可能下一轮就被用户否掉，提前写进长期记忆
    会让后续所有 Agent 都带着一条用户已经反悔的要求。
    """
    items: list[parsing.MemoryItem] = []
    seen: set[str] = set()
    for message in reversed(messages_of(project, conversation_id)):
        if message.role != "assistant":
            continue
        for item in parsing.parse_memories(message.content):
            key = memory_hash(item.kind, item.content)
            if key not in seen:
                seen.add(key)
                items.append(item)
    return tuple(items)


def write_memory(
    project: Session,
    kind: str,
    content: str,
    *,
    source: str | None = None,
    character_ref: str = PROJECT_SCOPE,
) -> ProjectMemory | None:
    """写一条项目记忆，已经有一样的就返回 None。

    去重按「类别 + 归一化内容」，因为同一条偏好在不同轮里措辞会差一个标点，靠原文比对
    会攒出一堆近似重复，注入时全都占预算。

    角色级那一档还要让着项目级：项目级已经有同一句时不再写副本——两条一模一样的记忆同时
    注入，用户在设置页关掉其中一条会发现它依旧生效。
    """
    key = memory_hash(kind, content)
    scopes = {PROJECT_SCOPE, character_ref}
    exists = project.scalars(
        select(ProjectMemory).where(
            ProjectMemory.content_hash == key,
            ProjectMemory.character_ref.in_(sorted(scopes)),
        )
    ).first()
    if exists is not None:
        return None
    row = ProjectMemory(
        id=uuid.uuid4().hex,
        kind=kind,
        content=content,
        content_hash=key,
        character_ref=character_ref,
        source_conversation_id=source,
    )
    project.add(row)
    project.flush()
    return row


def _write_memories(
    project: Session,
    conversation: Conversation,
    items: Sequence[parsing.MemoryItem],
) -> tuple[str, ...]:
    """去重后追写 project_memory，返回真正新增的内容。作用域跟会话的对焦对象一致。"""
    scope = memory_scope(conversation)
    added: list[str] = []
    for item in items:
        row = write_memory(
            project, item.kind, item.content, source=conversation.id, character_ref=scope
        )
        if row is not None:
            added.append(row.content)
    return tuple(added)


def commit(
    project: Session,
    ref: ProjectRef,
    conversation: Conversation,
    *,
    draft_ids: Sequence[str] | None = None,
) -> CommitResult:
    """确认沉淀：草稿写定稿位，会话收口，关键决策进长期记忆。"""
    if conversation.status != "active":
        raise Conflict("这个会话已经结束了，不能再沉淀")

    pending = drafts_of(project, conversation.id)
    if draft_ids is not None:
        wanted = set(draft_ids)
        pending = [d for d in pending if d.id in wanted]
        missing = wanted - {d.id for d in pending}
        if missing:
            raise NotFound(f"草稿 {sorted(missing)} 不在这个会话的待确认列表里")
    if not pending:
        raise Conflict("这个会话还没有待确认的草稿")

    archived: list[archive.ArchiveResult] = []
    for draft in pending:
        result = archive.commit_draft(
            ref,
            target_path=draft.target_path,
            content=draft.content,
            based_on_hash=draft.based_on_hash,
            conversation_id=conversation.id,
        )
        draft.status = "committed"
        archived.append(result)
        record_event(
            project,
            conversation.id,
            "artifact_committed",
            f"沉淀 {result.target_path}",
            {
                "target_path": result.target_path,
                "content_hash": result.content_hash,
                "previous_path": result.previous_path,
                "agent_code": conversation.agent_code,
            },
        )

    added = _write_memories(project, conversation, harvest_memories(project, conversation.id))
    _link_spec(project, conversation, archived)
    conversation.status = "committed"
    project.commit()

    BUS.publish(
        conversation.id,
        COMMITTED,
        {"targets": [r.target_path for r in archived], "memories_added": list(added)},
    )
    _log.info(
        "conversation_committed",
        conversation=conversation.id,
        targets=[r.target_path for r in archived],
        memories_added=len(added),
    )
    return CommitResult(
        conversation_id=conversation.id,
        archived=tuple(archived),
        memories_added=added,
    )


def _link_spec(
    project: Session, conversation: Conversation, archived: Sequence[archive.ArchiveResult]
) -> None:
    """角色会话沉淀设定文档后，把路径记回 `characters.spec_path`。

    只记路径不动 `state`：状态推进有门禁（设定要过 `spec_reviewer` 才算 S1），沉淀一份
    文档不等于过审。
    """
    if conversation.target_kind != "character" or not archived:
        return
    character = project.get(Character, conversation.target_ref)
    if character is None:
        return
    for result in archived:
        if result.target_path.endswith(".md"):
            character.spec_path = result.target_path
            return


def discard(project: Session, conversation: Conversation) -> int:
    """丢弃草稿：只改状态，会话与消息全留着可回看。"""
    if conversation.status == "committed":
        raise Conflict("已经沉淀过的会话不能再丢弃")
    pending = drafts_of(project, conversation.id)
    for draft in pending:
        draft.status = "discarded"
    conversation.status = "discarded"
    project.commit()
    _log.info("conversation_discarded", conversation=conversation.id, drafts=len(pending))
    return len(pending)
