"""会话引擎：一轮对话从收到用户输入到落库的完整过程。

职责边界：

- 上下文怎么拼由 `context` 决定，本模块只负责喂给它库里的东西
- 用哪个候选由 `providers.router` 决定，本模块只把会话行当粘性绑定传进去
- 落盘由 `assets.archive` 决定，本模块只在用户确认时调它

两条不能松的规则：

1. **未确认不落盘**。产物只进 `artifact_drafts`；确认沉淀是唯一的写工作区入口。
2. **原文只折不删**。超预算时把最老的消息压缩进摘要并标 `folded=true`，`messages` 里
   的原文永远留着，前端展开还能看到当时到底说了什么。

消息流在库里、记忆在目录里：前者是过程记录，删了重跑就有；后者是跟用户谈成的共识，得跟着
对象进 Git 给下一个接手的人看，读写都走 `assets.memory`。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from atelier.agents import audit, context, dispatch, orchestrator, parsing, tokens
from atelier.agents.definitions import AgentDefinition, get_agent
from atelier.agents.stream_bus import BUS, COMMITTED, DELTA, ERROR, TURN
from atelier.assets import archive, characters, layout, projects
from atelier.assets import memory as memory_files
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import (
    ArtifactDraft,
    Character,
    Conversation,
    Message,
)
from atelier.db.task_events import record as record_event
from atelier.errors import Conflict, Interrupted, NotFound
from atelier.providers import router, text_chat
from atelier.providers.base import Candidate, Decision, ProviderError
from atelier.settings import get_settings

_log = structlog.get_logger(__name__)

TARGET_KINDS = ("project", "character")

THINKING = "thinking"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
"""assistant 消息的四种下场。`THINKING` 不进上下文（里面一个字也没有），炸了与被中断的换成占位。"""

FAILED_TURN_PLACEHOLDER = "（这一轮调用失败，没有给出回答）"

INTERRUPTED_REASON = "这一轮被你中断了"

MAX_FOLD_ROUNDS = 5
"""一轮里最多折几次。

正常一两次就够；卡在这个上限说明单条消息本身就超预算（用户粘了一整篇文档），继续折也
压不下来，此时宁可带着超预算发出去让供应商报错，也比无声地空转几十次好。
"""

MAX_AUTO_CONTINUATIONS = 2
"""主回答被输出上限截断后最多自动续写几次，避免异常模型无限消耗额度。"""

MAX_AUTO_OUTPUT_TOKENS = 32768
"""自动续写逐次翻倍时的安全上限；模型原配置更高时保持原配置，不反向缩小。"""

AUTO_CONTINUE_PROMPT = (
    "上一段回答因输出长度限制被截断。请从截断处直接继续，只输出尚未完成的内容，"
    "不要重复已经输出的文字，也不要解释续写过程。"
)

AUTO_RETRY_EMPTY_PROMPT = (
    "上一次生成因输出长度限制结束，并且没有产生任何可见正文。请停止扩展内部分析，"
    "直接给出完整答复；优先完成规定的结构化内容。"
)

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
    choices: tuple[parsing.ChoiceGroup, ...] = ()
    """这轮要用户拍板的那几组选项，前端摆成选择组件。"""


@dataclass(frozen=True, slots=True)
class CommitResult:
    """一次确认沉淀的结果。"""

    conversation_id: str
    archived: tuple[archive.ArchiveResult, ...] = ()
    memories_added: tuple[str, ...] = ()
    """新写进对象目录里偏好文件的内容，去重后剩下的那些。"""


@dataclass(slots=True)
class ContextInputs:
    """组装一轮上下文所需的库内材料。"""

    agent: AgentDefinition
    addendum: str | None = None
    artifact_path: str | None = None
    artifact_text: str | None = None
    art_bible_path: str | None = None
    art_bible_text: str | None = None
    config_path: str | None = None
    config_text: str | None = None
    project_memories: list[memory_files.MemoryEntry] = field(default_factory=list)
    memory: memory_files.AgentMemory | None = None
    messages: list[context.MessageLike] = field(default_factory=list)
    """进上下文的那几条。失败轮在这里是占位而不是库里的原行，所以只按协议读，不当 ORM 用。"""
    rows: dict[int, Message] = field(default_factory=dict)
    """turn_no 到库里真实行的映射。折叠要把 `folded` 落到真实行上，改占位副本是白改。"""


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #


def conversation_id_for_character(character_id: str) -> str:
    """角色会话 ID 只由角色 ID 决定，不依赖目录、查询历史或进程缓存。"""
    return hashlib.md5(character_id.encode(), usedforsecurity=False).hexdigest()


def start(
    project: Session,
    *,
    agent_code: str,
    target_kind: str,
    target_ref: str | None = None,
    title: str = "",
) -> Conversation:
    """开一个新会话。

    项目会话每次使用随机 ID；角色是一物一会话，ID 只由角色 ID 确定性推导。角色若已有会话，
    调用方应走 `ensure()` 接回原会话，而不是另建一条并行消息流。
    """
    agent = get_agent(agent_code)
    if not agent.conversational:
        raise Conflict(f"{agent_code} 不是会话型 Agent，不能开会话")
    if target_kind not in TARGET_KINDS:
        raise Conflict(f"target_kind 只能是 {TARGET_KINDS} 之一")
    if target_kind == "character" and not target_ref:
        raise Conflict("角色会话必须指明是哪个角色")

    conversation_id = (
        conversation_id_for_character(target_ref)
        if target_kind == "character" and target_ref is not None
        else uuid.uuid4().hex
    )
    if project.get(Conversation, conversation_id) is not None:
        raise Conflict(f"角色 {target_ref} 已有会话")
    conversation = Conversation(
        id=conversation_id,
        target_kind=target_kind,
        target_ref=target_ref,
        agent_code=agent_code,
        title=title.strip()[:255],
        status="active",
    )
    project.add(conversation)
    project.commit()
    _log.info("conversation_started", id=conversation.id, agent=agent_code, target=target_ref)
    return conversation


def get(project: Session, conversation_id: str) -> Conversation:
    conversation = project.get(Conversation, conversation_id)
    if conversation is None:
        raise NotFound(f"会话 {conversation_id} 不存在")
    return conversation


def _adopt_conversation_id(
    project: Session, conversation: Conversation, conversation_id: str
) -> Conversation:
    """把旧版随机会话 ID 原地迁到由角色 ID 推导出的固定值。"""
    old_id = conversation.id
    project.execute(
        update(Message)
        .where(Message.conversation_id == old_id)
        .values(conversation_id=conversation_id)
    )
    project.execute(
        update(ArtifactDraft)
        .where(ArtifactDraft.conversation_id == old_id)
        .values(conversation_id=conversation_id)
    )
    conversation.id = conversation_id
    project.commit()
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

    接着最近那场聊，不看它沉淀过没有：沉淀只是把草稿写进定稿位，聊到哪儿、定了什么都还在这
    场里，为此另起一场等于把上下文丢掉重讲一遍。

    一个对焦对象（一个项目、一个角色、以后一张地图）就一场会话，`agent_code` 这一维指的是这场的
    **主 Agent**；一场里要跑多个 Agent 不开新会话，而是由主 Agent 指派，见
    `agents/orchestrator.py`。所以这里不比 `agent_code`：同一个对象上已经有一场就接着聊，库里
    那个主 Agent 说了算——换个 Agent 就另开一场的话，两场会各自满上一半上下文。
    """
    if target_kind == "character" and target_ref:
        conversation_id = conversation_id_for_character(target_ref)
        current = project.get(Conversation, conversation_id)
        if current is not None:
            if current.target_kind != target_kind or current.target_ref != target_ref:
                raise Conflict(f"角色 {target_ref} 的固定会话 ID 已被其他对象占用")
            return current
        rows = list_conversations(project, target_kind=target_kind, target_ref=target_ref)
        if rows:
            return _adopt_conversation_id(project, rows[0], conversation_id)
    else:
        rows = list_conversations(project, target_kind=target_kind, target_ref=target_ref)
        if rows:
            return rows[0]
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


def target_dir(project: Session, ref: ProjectRef, conversation: Conversation) -> Path:
    """这场会话的共识落在哪个目录：项目会话在项目根，角色会话在那个角色自己的目录里。

    跟审计记录同一套定位：记忆是对着这个对象谈出来的，角色改名搬目录时它得跟着走。
    """
    if conversation.target_kind == "character":
        return _scope_dir(project, ref, conversation.target_ref or "")
    return ref.dir


def _scope_dir(project: Session, ref: ProjectRef, character_ref: str) -> Path:
    """这一档记忆存在哪里。空作用域是项目级。"""
    if not character_ref:
        return ref.dir
    return ref.absolute(_character(project, character_ref).dir_name)


def agent_memory_of(
    project: Session, ref: ProjectRef, conversation: Conversation, agent_code: str = ""
) -> memory_files.AgentMemory:
    """某个 Agent 在这场会话里记下的东西。文件还没有就是空的一份，不预建。

    按 Agent 分文件而不是一场一份：一场会话里多个 Agent 各记各的，混在一份里下一个 Agent
    会把别人的待确认问题当成自己的活儿。不传 `agent_code` 就是当下在说话的那个。
    """
    code = agent_code or orchestrator.actor_for(conversation)
    return memory_files.read_agent_memory(target_dir(project, ref, conversation), code)


def write_agent_memory(
    project: Session,
    ref: ProjectRef,
    conversation: Conversation,
    memory: memory_files.AgentMemory,
    agent_code: str = "",
) -> None:
    """把这份记忆整份写回对象目录。"""
    code = agent_code or orchestrator.actor_for(conversation)
    memory_files.write_agent_memory(
        target_dir(project, ref, conversation), code, memory, role=get_agent(code).role
    )


def choices_of(project: Session, conversation_id: str) -> tuple[parsing.ChoiceGroup, ...]:
    """这场会话当下要用户拍板的那几组选项。现场从消息里解析，不建表。

    只认最后一条消息：用户答过之后那一轮的回话里没再提新分歧，就说明这几处已经拍完了，把
    上一轮的表单继续摆成可点的样子等于请用户再选一遍已经定了的事。定下的结果本来就在
    用户自己那条消息里摆着。
    """
    row = project.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.turn_no.desc())
        .limit(1)
    ).first()
    if row is None or row.role != "assistant":
        return ()
    return parsing.parse_choices(row.content)


def naming_of(project: Session, conversation_id: str) -> tuple[parsing.NamingOption, ...]:
    """这场会话目前的命名建议：从最近一条带过该块的助手消息里现场解析。

    不建表也不加列：建议本身就存在消息原文里，再存一份就多一处要对账的状态。只认最近那
    一条：聊到后面模型会改主意，把历史建议堆在一起只会让用户面对十几个过期选项。

    没落盘过就一律当没有：立项分两段——先对焦需求、确认美术风格，落盘之后才轮到定项目名。
    模型有时会抢在前面先把名字报出来，照着发出去用户就会在风格还没拍完的时候看见「确认立项」。
    """
    if not is_settled(project, conversation_id):
        return ()
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


def is_settled(project: Session, conversation_id: str) -> bool:
    """这场会话落过盘没有。前端拿它分阶段，所以看库里的既成事实而不是本地那点内存标记：
    切走页面、重开进程都还认得出来聊到哪一步了。"""
    return bool(drafts_of(project, conversation_id, status="committed"))


PROJECT_SCOPE = ""
"""项目级记忆的作用域。空串而不是 None：它要跟角色 id 放在同一个变量里比。"""


def memory_scope(conversation: Conversation) -> str:
    """这场会话里聊出来的记忆归谁。

    看的是对焦对象，不是 Agent 声明的 `memory_scope`：同一个 `spec_writer` 在不同角色上聊出
    的偏好本来就不能混在一起，而声明只能说清它该**看到**哪一档。
    """
    if conversation.target_kind == "character" and conversation.target_ref:
        return conversation.target_ref
    return PROJECT_SCOPE


def enabled_memories(
    project: Session, ref: ProjectRef, character_ref: str = PROJECT_SCOPE
) -> list[memory_files.MemoryEntry]:
    """该注入的记忆：项目级的总带上，再加当前角色自己那些，项目级在前。

    别的角色那几条不带：「赤瞳的尾巴要 2 条」对下一个角色不仅无用，还会被模型当成本项目的
    通例写进新设定，用户得花一轮把它推翻。
    """
    entries = [one for one in memory_files.read_preferences(ref.dir) if one.enabled]
    if not character_ref:
        return entries
    seen = {one.id for one in entries}
    entries.extend(
        one
        for one in memory_files.read_preferences(_scope_dir(project, ref, character_ref))
        if one.enabled and one.id not in seen
    )
    return entries


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


def addendum(ref: ProjectRef, agent_code: str) -> str | None:
    """项目级附加指令，读 `prompts/agents/{agent_code}.md`。

    单次调用型 Agent 也要带上——不带就成了评审按的标准跟创作按的标准不一样。
    """
    prompt = memory_files.read_agent_prompt(ref.dir, agent_code)
    if prompt is None or not prompt.enabled:
        return None
    return prompt.content


def _art_bible_for(ref: ProjectRef, conversation: Conversation) -> tuple[str | None, str | None]:
    """这场会话要额外带上的项目美术规范。

    只给角色这类子目标的会话：立项会话的定稿本身就是 `art-bible.md`，再带一份就是同一段文字
    占两份预算，而且两段内容一旦不一致（草稿还没沉淀时就会）模型不知道该信哪一份。

    角色会话必须带：角色设计得跟项目世界观对得上，而那份只写在 art-bible 里。不带的后果是
    模型只能凭用户那几句话自己编一套风格，出来的设定看着没错但跟项目对不上。
    """
    if conversation.target_kind == "project":
        return None, None
    config = projects.read_config(ref.dir)
    path = layout.art_bible_path(ref.dir, config.art_bible)
    if not path.is_file():
        return None, None
    return config.art_bible, path.read_text(encoding="utf-8")


@dataclass(slots=True)
class _FailedTurn:
    """失败轮在上下文里的占位。

    不拿 ORM 行改一份副本：库里那行存的是当时的错误文本（用户展开还要看），而游离的 ORM
    对象拖在手里只会让人分不清哪一份会落库。
    """

    turn_no: int
    role: str
    content: str
    folded: bool


def _placeholder_of(message: Message) -> _FailedTurn:
    return _FailedTurn(
        turn_no=message.turn_no,
        role=message.role,
        content=FAILED_TURN_PLACEHOLDER,
        folded=message.folded,
    )


def _history_for_context(messages: list[Message]) -> list[context.MessageLike]:
    """进上下文的历史消息。

    没回完的（thinking）丢掉：它的 content 本来就是空的，发过去就是一句空回答。

    炸了、被中断的不能跟着丢——那一轮的 user 消息是 DONE、assistant 是 FAILED，只滤后者会留下
    连续几条 user 说话、一条回答也没有的序列，模型看到这种很容易把上一句当成被忽略。改成把
    回答换成一句固定占位：交替还在，模型也看得出那一轮确实没给出东西。
    """
    kept: list[context.MessageLike] = []
    for one in messages:
        if one.status == THINKING:
            continue
        if one.status in (FAILED, CANCELLED) and one.role == "assistant":
            kept.append(_placeholder_of(one))
            continue
        kept.append(one)
    return kept


def _inputs(project: Session, ref: ProjectRef, conversation: Conversation) -> ContextInputs:
    agent = get_agent(orchestrator.actor_for(conversation))
    artifact_path, artifact_text = artifact_of(project, ref, conversation)
    art_bible_path, art_bible_text = _art_bible_for(ref, conversation)
    history = messages_of(project, conversation.id)
    # 只有立项会话改得动 project.json，角色会话看见它也用不上，白占预算
    is_project = conversation.target_kind == "project"
    return ContextInputs(
        agent=agent,
        addendum=addendum(ref, agent.agent_code),
        artifact_path=artifact_path,
        artifact_text=artifact_text,
        art_bible_path=art_bible_path,
        art_bible_text=art_bible_text,
        config_path=layout.PROJECT_JSON if is_project else None,
        config_text=config_snapshot(ref) if is_project else None,
        project_memories=enabled_memories(project, ref, memory_scope(conversation)),
        memory=agent_memory_of(project, ref, conversation),
        messages=_history_for_context(history),
        rows={one.turn_no: one for one in history},
    )


def _assemble(inputs: ContextInputs, candidate: Candidate | None = None) -> context.Assembled:
    """本轮的消息与预算。

    得知道这一轮落在哪个候选上：上下文预算按那个模型的窗口算，换了候选预算也跟着变。
    拿不到候选（测试里直接拼上下文）就回落 Agent 自己写的保守预算。
    """
    settings = get_settings()
    window = candidate.params.get("context_window") if candidate is not None else None
    return context.assemble(
        inputs.agent,
        inputs.messages,
        addendum=inputs.addendum,
        artifact_path=inputs.artifact_path,
        artifact_text=inputs.artifact_text,
        art_bible_path=inputs.art_bible_path,
        art_bible_text=inputs.art_bible_text,
        config_path=inputs.config_path,
        config_text=inputs.config_text,
        project_memories=inputs.project_memories,
        memory=inputs.memory,
        recent_turns=settings.recent_turns,
        budget=context.effective_budget(
            inputs.agent,
            int(window) if isinstance(window, int | float) else None,
            settings.context_budget_ratio,
        ),
    )


def _next_turn_no(project: Session, conversation_id: str) -> int:
    current = project.scalar(
        select(func.max(Message.turn_no)).where(Message.conversation_id == conversation_id)
    )
    return int(current or 0) + 1


def _add_message(
    project: Session,
    conversation: Conversation,
    role: str,
    content: str,
    token_count: int,
    *,
    status: str = DONE,
) -> Message:
    message = Message(
        conversation_id=conversation.id,
        turn_no=_next_turn_no(project, conversation.id),
        role=role,
        content=content,
        token_count=token_count,
        status=status,
        # 谁说的话记在谁名下。现在恒是会话主 Agent，将来主 Agent 派了子 Agent，执行器写
        # 子 Agent 的 code，见 agents/orchestrator.py
        agent_code="" if role == "user" else orchestrator.actor_for(conversation),
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
    body = text.strip()
    if not body:
        raise Conflict("发给 Agent 的内容不能为空")

    agent = get_agent(orchestrator.actor_for(conversation))
    if not agent.conversational:
        raise Conflict(f"{agent.agent_code} 不是会话型 Agent")

    # 上一轮的增量与它末尾那条 turn 得先清掉：留着会把这一轮新订上来的流当场收掉
    BUS.reset(conversation.id)
    # 上一轮被中断过就会留下标记，不清新这一轮一开口就被它掐了
    BUS.clear_cancel(conversation.id)

    _add_message(project, conversation, "user", body, tokens.estimate_text(body))
    # 先落一条空的 assistant：「正在想」得能活过切页面与重启，只活在流里的状态一离开这一屏就没了
    assistant = _add_message(project, conversation, "assistant", "", 0, status=THINKING)
    project.commit()

    turn_audit = _turn_audit(project, ref, conversation, assistant.turn_no, agent.agent_code)
    inputs = _inputs(project, ref, conversation)
    try:
        decision = _select(runtime, project, ref, conversation, agent)

        folded = _fold_until_fits(
            project, runtime, ref, conversation, inputs, decision, caller, turn_audit
        )
        assembled = _assemble(inputs, decision.candidate)

        on_delta = _delta_publisher(conversation.id) if stream else None
        reply = _call(
            runtime,
            project,
            ref,
            conversation,
            agent,
            decision,
            assembled,
            caller,
            on_delta,
            turn_audit,
        )

        # 空回答不在这里拦：它得算这个候选没干成活儿，得在 dispatch 里招下一个候选重试
        content = reply.content.strip()
    except Exception as exc:
        # 炸了也要说一声：订流的那头只认这条广播，不发它前端就一直等着字出现
        if isinstance(exc, Interrupted) or BUS.cancel_requested(conversation.id):
            # 中断那一路已经把占位改成 cancelled，这里再写一次就是把用户的决定改回来
            BUS.clear_cancel(conversation.id)
            raise Interrupted(INTERRUPTED_REASON) from exc
        assistant.content = str(exc)[:500]
        assistant.status = FAILED
        project.commit()
        BUS.publish(conversation.id, ERROR, str(exc))
        raise

    assistant.content = content
    assistant.token_count = reply.completion_tokens or tokens.estimate_text(content)
    assistant.status = DONE
    parsed = parsing.parse_turn(content)
    if _apply_progress(inputs.memory, parsed.progress):
        write_agent_memory(project, ref, conversation, inputs.memory)
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
        # 落盘之前报的名字不往外发，理由见 naming_of
        naming=parsed.naming if is_settled(project, conversation.id) else (),
        choices=parsed.choices,
    )
    BUS.publish(conversation.id, TURN, {"turn_no": assistant.turn_no, "drafts": list(draft_ids)})
    return result


def _turn_audit(
    project: Session,
    ref: ProjectRef,
    conversation: Conversation,
    turn_no: int,
    agent_code: str,
) -> audit.TurnAudit | None:
    """配置开启时按会话目标确定审计目录；关闭时不碰磁盘。"""
    if not projects.read_config(ref.dir).conversation_audit:
        return None

    target_dir = ref.dir
    target = "project"
    if conversation.target_kind == "character":
        character = characters.get(project, conversation.target_ref or "")
        target_dir = ref.absolute(character.dir_name)
        target = f"character:{character.id}"

    return audit.TurnAudit.create(
        target_dir,
        conversation_id=conversation.id,
        turn_no=turn_no,
        target=target,
        agent_code=agent_code,
    )


def _delta_publisher(conversation_id: str) -> Callable[[str], None]:
    def publish(piece: str) -> None:
        # 中断就靠这里：回调里抛出去会一路把服务商那条 HTTP 流关掉，剩下的字不再生也不再计费
        if BUS.cancel_requested(conversation_id):
            raise Interrupted(INTERRUPTED_REASON)
        BUS.publish(conversation_id, DELTA, piece)

    return publish


def interrupt(project: Session, conversation: Conversation) -> bool:
    """中断这一轮：占位消息标成 `cancelled`，推理还在跑就下一段增量时停。

    重启后卡在库里的那条 `thinking` 也靠它清：进程都换了一个，推理早没了，状态却还挂在那里。
    返回真说明确实有一轮被掐了，假就是本来就没在跑。
    """
    rows = list(
        project.scalars(
            select(Message).where(
                Message.conversation_id == conversation.id, Message.status == THINKING
            )
        )
    )
    if not rows:
        return False

    BUS.request_cancel(conversation.id)
    for row in rows:
        row.status = CANCELLED
    project.commit()
    # 订流的那头只认广播：不发这条，它要等到模型自己结束才知道这一轮已经不算了
    BUS.publish(conversation.id, ERROR, INTERRUPTED_REASON)
    return True


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


def _capture_deltas(received: list[str], publish: Callable[[str], None]) -> Callable[[str], None]:
    """流式片段一份写审计、一份照常广播。"""

    def capture(piece: str) -> None:
        received.append(piece)
        publish(piece)

    return capture


def _sum_reported(values: Sequence[int | None]) -> int | None:
    """多次调用的用量必须全部有供应商实报才能相加，缺一段就不猜。"""
    return sum(value for value in values if value is not None) if None not in values else None


def _merge_replies(replies: Sequence[text_chat.ChatReply]) -> text_chat.ChatReply:
    """把同一逻辑轮次的主回答与自动续写合成一份结果。"""
    last = replies[-1]
    return text_chat.ChatReply(
        content="".join(reply.content for reply in replies),
        prompt_tokens=_sum_reported(tuple(reply.prompt_tokens for reply in replies)),
        completion_tokens=_sum_reported(tuple(reply.completion_tokens for reply in replies)),
        total_tokens=_sum_reported(tuple(reply.total_tokens for reply in replies)),
        remaining=last.remaining,
        latency_ms=sum(reply.latency_ms for reply in replies),
        finish_reason=last.finish_reason,
        reasoning="".join(reply.reasoning for reply in replies),
    )


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
    turn_audit: audit.TurnAudit | None,
) -> text_chat.ChatReply:
    """发出一轮完整回答；被输出上限截断时在同一轮内自动续写并合并。

    每段仍分别经过 `dispatch` 记账并写入同一份审计文件。续写只存在于本次 provider payload，
    不伪造成用户消息落库；全部完成后，上层才解析待选项、草稿与记忆。
    """

    def rebind(error: ProviderError) -> Decision:
        picked = _select(runtime, project, ref, conversation, agent)
        # 选回同一个候选就不写原因：绑定根本没换，记上只会让人以为刚才换过人
        if picked.candidate.provider_model_id != decision.candidate.provider_model_id:
            conversation.rebind_reason = str(error)[:255]
            project.commit()
        return picked

    base_payload = assembled.payload()
    payload = base_payload
    replies: list[text_chat.ChatReply] = []

    for continuation_no in range(MAX_AUTO_CONTINUATIONS + 1):
        purpose = "主回答" if continuation_no == 0 else f"自动续写 {continuation_no}"
        configured_max_tokens = text_chat.output_budget(
            decision.candidate, agent.max_output_tokens
        )
        max_tokens = min(
            configured_max_tokens * (2**continuation_no),
            max(configured_max_tokens, MAX_AUTO_OUTPUT_TOKENS),
        )
        partial: list[str] = []
        effective_delta = on_delta
        if turn_audit is not None:
            turn_audit.write_request(
                purpose,
                decision.candidate,
                payload,
                max_tokens=max_tokens,
            )
            if on_delta is not None:
                effective_delta = _capture_deltas(partial, on_delta)

        try:
            reply = dispatch.call(
                runtime,
                agent.agent_code,
                decision,
                payload,
                chat,
                project_code=ref.code,
                on_delta=effective_delta,
                reselect=rebind,
                allow_truncated_empty=True,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            if turn_audit is not None:
                turn_audit.write_error(exc, "".join(partial))
            raise

        if turn_audit is not None:
            turn_audit.write_response(reply)
        replies.append(reply)
        if not reply.truncated:
            return _merge_replies(replies)
        if continuation_no == MAX_AUTO_CONTINUATIONS:
            raise ProviderError(
                f"AI 连续 {MAX_AUTO_CONTINUATIONS + 1} 次达到输出上限，自动续写仍未完成"
            )

        continued_content = "".join(item.content for item in replies)
        payload = [*base_payload]
        if continued_content:
            payload.append({"role": "assistant", "content": continued_content})
        payload.append(
            {
                "role": "user",
                "content": AUTO_CONTINUE_PROMPT
                if continued_content
                else AUTO_RETRY_EMPTY_PROMPT,
            }
        )
        decision = _select(runtime, project, ref, conversation, agent)

    raise AssertionError("自动续写循环未返回")


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
    turn_audit: audit.TurnAudit | None,
) -> tuple[int, ...]:
    """超预算就把最老的原文压缩进摘要，直到装得下或折不动为止。"""
    memory = agent_memory_of(project, ref, conversation)
    recent_turns = get_settings().recent_turns
    folded: list[int] = []

    for _ in range(MAX_FOLD_ROUNDS):
        assembled = _assemble(inputs, decision.candidate)
        # 窗口外的一定要折（不折就真丢了），超预算那批跟它们合成一次压缩，省一轮调用
        plan = set(context.overflow_turns(inputs.messages, recent_turns))
        plan.update(context.fold_plan(assembled))
        if not plan:
            break

        victims = [m for m in inputs.messages if m.turn_no in plan and not m.folded]
        if not victims:
            break

        request = context.fold_request(victims, memory.summary)
        payload = [
            {
                "role": "system",
                "content": FOLD_SUMMARY_SYSTEM.format(role=inputs.agent.role),
            },
            {"role": "user", "content": request},
        ]
        if turn_audit is not None:
            turn_audit.write_request(
                "上下文折叠",
                decision.candidate,
                payload,
                max_tokens=text_chat.output_budget(decision.candidate, None),
            )
        try:
            # 走 dispatch 而不是直接 chat：折叠跟主回答一样会遇限流与空回答，自己发就没了重试、
            # 换候选与记账。候选变了不回写会话绑定：这只是一次内部压缩，不该改变主对话粘在谁身上。
            reply = dispatch.call(
                runtime,
                inputs.agent.agent_code,
                decision,
                payload,
                chat,
                project_code=ref.code,
            )
        except Exception as exc:
            if turn_audit is not None:
                turn_audit.write_error(exc)
            raise
        if turn_audit is not None:
            turn_audit.write_response(reply)

        memory.summary = reply.content.strip()
        memory.folded_turns += len(victims)
        write_agent_memory(project, ref, conversation, memory)
        for message in victims:
            message.folded = True
            row = inputs.rows.get(message.turn_no)
            if row is not None:
                row.folded = True
        folded.extend(m.turn_no for m in victims)
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


def _apply_progress(
    memory: memory_files.AgentMemory | None, progress: parsing.Progress | None
) -> bool:
    """把这轮的进度并进会话记忆，真改动了东西返回真（调用方据此决定要不要重写文件）。

    结论累加、开放问题整体替换：结论一旦拍板就不该因为某轮忘了复述而消失，而开放问题的
    最新一份才是准的——上一轮问完的问题这轮不该还挂着。
    """
    if memory is None or progress is None:
        return False
    changed = False
    if progress.decisions:
        merged = _merge_unique(memory.decisions, progress.decisions)
        changed = merged != memory.decisions
        memory.decisions = merged
    if progress.open_questions:
        incoming = list(progress.open_questions)
        changed = changed or incoming != memory.open_questions
        memory.open_questions = incoming
    return changed


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
    """记忆去重的键。跟落盘那边共一套算法，否则本模块去了重、写进文件又算出另一个 id。"""
    return memory_files.memory_hash(kind, content)


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
    ref: ProjectRef,
    kind: str,
    content: str,
    *,
    character_ref: str = PROJECT_SCOPE,
) -> memory_files.MemoryEntry | None:
    """写一条项目记忆进对象目录，已经有一样的就返回 None。

    去重按「类别 + 归一化内容」，因为同一条偏好在不同轮里措辞会差一个标点，靠原文比对
    会攒出一堆近似重复，注入时全都占预算。

    角色级那一档还要让着项目级：项目级已经有同一句时不再写副本——两条一模一样的记忆同时
    注入，用户在设置页关掉其中一条会发现它依旧生效。
    """
    taken: set[str] = set()
    scope = memory_files.SCOPE_PROJECT
    if character_ref:
        taken = {one.id for one in memory_files.read_preferences(ref.dir)}
        scope = memory_files.SCOPE_CHARACTER
    return memory_files.add_preference(
        _scope_dir(project, ref, character_ref), kind, content, scope=scope, taken=taken
    )


def _write_memories(
    project: Session,
    ref: ProjectRef,
    conversation: Conversation,
    items: Sequence[parsing.MemoryItem],
) -> tuple[str, ...]:
    """去重后追写对象目录里的偏好文件，返回真正新增的内容。作用域跟会话的对焦对象一致。"""
    scope = memory_scope(conversation)
    added: list[str] = []
    for item in items:
        entry = write_memory(project, ref, item.kind, item.content, character_ref=scope)
        if entry is not None:
            added.append(entry.content)
    return tuple(added)


def commit(
    project: Session,
    ref: ProjectRef,
    conversation: Conversation,
    *,
    draft_ids: Sequence[str] | None = None,
) -> CommitResult:
    """确认沉淀：草稿写定稿位，关键决策进长期记忆。

    不收口会话：沉淀是「这一版落盘」，不是「这场聊完了」。接着聊出下一版再沉淀一次就是了。
    """
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

    added = _write_memories(project, ref, conversation, harvest_memories(project, conversation.id))
    _link_spec(project, conversation, archived)
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
    """丢弃这批草稿：只标弃用，磁盘不动，会话继续开着。

    丢的是这一版写出来的东西，不是这场对话——用户按的那个按钮写的就是「丢弃草稿」。
    """
    pending = drafts_of(project, conversation.id)
    for draft in pending:
        draft.status = "discarded"
    project.commit()
    _log.info("conversation_discarded", conversation=conversation.id, drafts=len(pending))
    return len(pending)
