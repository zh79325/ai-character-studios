"""会话引擎：一轮对话、折叠、草稿、确认沉淀、会话级粘性绑定。

A6 的三条验收标准都钉在这里：

1. 长会话不爆上下文（折叠后原文仍在库里）
2. 未确认时磁盘无改动
3. 确认后定稿落盘且旧版进 `tmp/`

模型一律用 `ScriptedChat` 替掉：这一层要验的是编排，不是模型会不会好好说话。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from atelier.agents import conversation as engine
from atelier.assets import archive, layout
from atelier.assets.layout import LayoutError
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import (
    ArtifactDraft,
    Character,
    Conversation,
    ProjectMemory,
    TaskEvent,
)
from atelier.db.runtime_models import RouteLog
from atelier.errors import Conflict, Interrupted, NotFound
from atelier.providers.base import NoCandidateError
from tests.conftest import ScriptedChat, bind_text_model

DESIGNER = "game_designer"
WRITER = "spec_writer"

DRAFT_REPLY = """明白了，先给一版。

[对焦进度]
已定：题材是赛博朋克
待定：面数预算
下一步：确认目标平台

[草稿开始: art-bible.md]
# 视觉规范
冷光下的湿滑金属。
[草稿结束]

[项目记忆]
preference: 喜欢冷色调
taboo: 不要蒸汽朋克齿轮
"""


@pytest.fixture
def candidate(session: Session) -> None:
    bind_text_model(session, DESIGNER)


def start_project_talk(project_db: Session, *, agent: str = DESIGNER) -> Conversation:
    return engine.start(project_db, agent_code=agent, target_kind="project", title="立项对焦")


def make_character(project_db: Session, name: str = "赤瞳") -> Character:
    character = Character(id=f"c-{name}", name=name, dir_name=f"characters/{name}")
    project_db.add(character)
    project_db.commit()
    return character


def send(
    project_db: Session,
    session: Session,
    project: ProjectRef,
    conversation: Conversation,
    text: str,
    chat: ScriptedChat,
    *,
    stream: bool = False,
) -> engine.TurnResult:
    return engine.send(project_db, session, project, conversation, text, chat=chat, stream=stream)


# --------------------------------------------------------------------------- #
# 开场
# --------------------------------------------------------------------------- #


def test_开会话时顺手建好会话记忆行(project_db: Session, project: ProjectRef) -> None:
    conversation = start_project_talk(project_db)

    assert conversation.status == "active"
    assert engine.memory_of(project_db, conversation.id).folded_turns == 0


def test_非会话型agent不能开会话(project_db: Session, project: ProjectRef) -> None:
    """单次调用型 Agent 没有前缀可复用，开会话只会攒一堆没人接着聊的空壳。"""
    with pytest.raises(Conflict, match="不是会话型"):
        engine.start(project_db, agent_code="prompt_smith", target_kind="project")


def test_角色会话必须指明角色(project_db: Session, project: ProjectRef) -> None:
    with pytest.raises(Conflict, match="哪个角色"):
        engine.start(project_db, agent_code=WRITER, target_kind="character")


def test_没有可用候选时报清楚(project_db: Session, project: ProjectRef, session: Session) -> None:
    conversation = start_project_talk(project_db)

    with pytest.raises(NoCandidateError):
        send(project_db, session, project, conversation, "开聊", ScriptedChat("好"))


# --------------------------------------------------------------------------- #
# 一轮对话
# --------------------------------------------------------------------------- #


def test_一轮对话记下两条消息与一份草稿(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    conversation = start_project_talk(project_db)
    chat = ScriptedChat(DRAFT_REPLY)

    result = send(project_db, session, project, conversation, "做个赛博朋克项目", chat)

    messages = engine.messages_of(project_db, conversation.id)
    assert [(m.turn_no, m.role) for m in messages] == [(1, "user"), (2, "assistant")]
    assert result.turn_no == 2
    assert len(result.draft_ids) == 1
    assert result.provider_label == "bailian/qwen-plus"
    # 用量以供应商为准，不用估算值
    assert result.completion_tokens == 20
    assert messages[1].token_count == 20


def test_上下文按固定顺序拼且定稿走system(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    """定稿与记忆是「事实」，混进对话序列模型会把它当上一轮发言去回应。"""
    project.absolute("art-bible.md").write_text("# 现有规范\n偏冷色。\n", encoding="utf-8")
    engine.write_memory(project_db, "taboo", "不要蒸汽朋克齿轮")
    project_db.commit()
    conversation = start_project_talk(project_db)
    chat = ScriptedChat("知道了。")

    send(project_db, session, project, conversation, "继续", chat)

    system, *rest = chat.calls[-1]
    assert system["role"] == "system"
    assert "# 现有规范" in system["content"]
    assert "不要蒸汽朋克齿轮" in system["content"]
    assert [m["role"] for m in rest] == ["user"]
    assert rest[0]["content"] == "继续"


def test_进度结论累加而开放问题整体替换(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    """拍板过的结论不该因为某轮忘了复述就消失；开放问题的最新一份才是准的。"""
    conversation = start_project_talk(project_db)
    chat = ScriptedChat(
        "[对焦进度]\n已定：题材定了\n待定：面数预算\n",
        "[对焦进度]\n已定：第三人称\n待定：目标平台\n",
    )

    send(project_db, session, project, conversation, "第一轮", chat)
    send(project_db, session, project, conversation, "第二轮", chat)

    memory = engine.memory_of(project_db, conversation.id)
    assert memory.decisions == ["题材定了", "第三人称"]
    assert memory.open_questions == ["目标平台"]


def test_空回答留一条炸了的记号而不是一句空话(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    """炸了也得留下痕：切走页面再回来的人得知道这一轮早就没了，而不是接着干等。"""
    conversation = start_project_talk(project_db)

    with pytest.raises(Exception, match="空回答"):
        send(project_db, session, project, conversation, "开聊", ScriptedChat("   "))

    rows = engine.messages_of(project_db, conversation.id)
    assert [m.role for m in rows] == ["user", "assistant"]
    assert rows[1].status == engine.FAILED
    assert "空回答" in rows[1].content


def test_跑的时候库里就摆着一条正在想(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    """占位得在调模型之前就落库，否则切走页面再回来就只剩一个空屏幕。"""
    conversation = start_project_talk(project_db)
    seen: list[tuple[str, str]] = []

    class Peeking(ScriptedChat):
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            rows = engine.messages_of(project_db, conversation.id)
            seen.extend((one.role, one.status) for one in rows)
            return super().__call__(*args, **kwargs)

    send(project_db, session, project, conversation, "开聊", Peeking("好"))

    assert seen == [("user", engine.DONE), ("assistant", engine.THINKING)]
    rows = engine.messages_of(project_db, conversation.id)
    assert [(m.role, m.status, m.content) for m in rows] == [
        ("user", engine.DONE, "开聊"),
        ("assistant", engine.DONE, "好"),
    ]


def test_中断把正在想那条标成取消并停下推理(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    """中断不删行：删了就只剩用户那句话孤零零摆在那里，看不出这一轮到底去哪了。"""
    conversation = start_project_talk(project_db)

    class Cutting(ScriptedChat):
        """字还没开始往外推用户就点了中断：下一段增量就应该抛出去，服务商那条流跟着关。"""

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            engine.interrupt(project_db, conversation)
            return super().__call__(*args, **kwargs)

    chat = Cutting("前半句\n后半句\n")
    with pytest.raises(Interrupted):
        engine.send(project_db, session, project, conversation, "开聊", chat=chat, stream=True)

    assert chat.deltas == ["前半句\n"]
    rows = engine.messages_of(project_db, conversation.id)
    assert [(m.role, m.status) for m in rows] == [
        ("user", engine.DONE),
        ("assistant", engine.CANCELLED),
    ]


def test_没在跑的时候中断什么也不改(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    conversation = start_project_talk(project_db)
    send(project_db, session, project, conversation, "开聊", ScriptedChat("好"))

    assert engine.interrupt(project_db, conversation) is False
    assert [m.status for m in engine.messages_of(project_db, conversation.id)] == [
        engine.DONE,
        engine.DONE,
    ]


def test_没回完的那几条不进下一轮的上下文(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    conversation = start_project_talk(project_db)
    with pytest.raises(Exception, match="空回答"):
        send(project_db, session, project, conversation, "先聊一句", ScriptedChat("   "))

    chat = ScriptedChat("这回答得上")
    send(project_db, session, project, conversation, "再聊一句", chat)

    assert all(one["content"] != "" for one in chat.calls[-1])
    assert not any("空回答" in one["content"] for one in chat.calls[-1])


def test_丢弃草稿后还能接着聊(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    conversation = start_project_talk(project_db)
    engine.discard(project_db, conversation)

    send(project_db, session, project, conversation, "再聊两句", ScriptedChat("好"))

    assert [m.role for m in engine.messages_of(project_db, conversation.id)] == [
        "user",
        "assistant",
    ]


CHOICE_REPLY = """先给几处要你定的。

[待选项]
- 项: 面数预算 / 选项: 8k | 15k | 30k / 推荐: 15k
"""


def test_待选项只认最后一条消息(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    """用户答过之后表单就该翻成结果，继续摆成可点的等于请他再选一遍定了的事。"""
    conversation = start_project_talk(project_db)
    chat = ScriptedChat(CHOICE_REPLY, "记下了。")
    send(project_db, session, project, conversation, "开聊", chat)

    assert [g.item for g in engine.choices_of(project_db, conversation.id)] == ["面数预算"]

    send(project_db, session, project, conversation, "这几项我定了：\n- 面数预算: 15k", chat)

    assert engine.choices_of(project_db, conversation.id) == ()


# --------------------------------------------------------------------------- #
# 会话级粘性绑定
# --------------------------------------------------------------------------- #


def test_同一会话每轮复用同一个候选(
    project_db: Session, project: ProjectRef, session: Session
) -> None:
    """换 provider 等于让对方从零算一遍前缀，多轮对话里 token 全白花。"""
    bind_text_model(session, DESIGNER, code="a", model_id="qwen-plus", priority=10)
    bind_text_model(session, DESIGNER, code="b", model_id="qwen-plus", priority=20)
    conversation = start_project_talk(project_db)
    chat = ScriptedChat("一", "二", "三")

    labels = [
        send(project_db, session, project, conversation, f"第{i}轮", chat).provider_label
        for i in range(1, 4)
    ]

    assert labels == ["a/qwen-plus"] * 3
    assert conversation.rebind_count == 0
    picks = [
        r.outcome
        for r in session.scalars(select(RouteLog).order_by(RouteLog.id))
        if r.outcome in ("bound", "sticky_hit", "rebound")
    ]
    assert picks == ["bound", "sticky_hit", "sticky_hit"]


def test_绑定落进项目库(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    """选路层拿的是全局库 Session，绑定要由这边提交，否则重启后会话就忘了绑过谁。"""
    conversation = start_project_talk(project_db)
    send(project_db, session, project, conversation, "开聊", ScriptedChat("好"))

    project_db.expire_all()
    reloaded = engine.get(project_db, conversation.id)
    assert reloaded.bound_provider_label == "bailian/qwen-plus"
    assert reloaded.bound_at is not None


# --------------------------------------------------------------------------- #
# 折叠：长会话不爆上下文
# --------------------------------------------------------------------------- #


def test_超预算时把最老的原文折进摘要而不删(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    candidate: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """截断会悄悄丢掉用户说过的话，所以只折不删：原文留在库里仍可展开回看。"""
    conversation = start_project_talk(project_db)
    chat = ScriptedChat()
    for i in range(1, 5):
        send(project_db, session, project, conversation, f"第{i}轮说的话" * 20, chat)

    # 把预算压到装不下，下一轮就必须先折
    agent = engine.get_agent(DESIGNER)
    monkeypatch.setattr(agent, "context_budget", 200)
    chat.replies = ["前情：聊过四轮，题材定了。", "接着说。"]

    result = send(project_db, session, project, conversation, "第五轮", chat)

    memory = engine.memory_of(project_db, conversation.id)
    assert result.folded_turns  # 真的折了
    assert memory.summary == "前情：聊过四轮，题材定了。"
    assert memory.folded_turns == len(result.folded_turns)

    folded = [m for m in engine.messages_of(project_db, conversation.id) if m.folded]
    assert [m.turn_no for m in folded] == list(result.folded_turns)
    assert "第1轮说的话" in folded[0].content  # 原文没被删

    # 折完这轮发出去的上下文里带上了摘要，且不再重复送折过的原文
    system = chat.system_of_last
    assert "前情：聊过四轮" in system
    sent = [m["content"] for m in chat.calls[-1][1:]]
    assert not any("第1轮说的话" in c for c in sent)


def test_最近两轮再超预算也不折(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    candidate: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """折到只剩摘要，Agent 就是在隔着一层转述回答用户刚说的话。"""
    conversation = start_project_talk(project_db)
    chat = ScriptedChat()
    for i in range(1, 3):
        send(project_db, session, project, conversation, f"很长的一段{i}" * 50, chat)

    monkeypatch.setattr(engine.get_agent(DESIGNER), "context_budget", 10)
    result = send(project_db, session, project, conversation, "再来一句", chat)

    live = [m for m in engine.messages_of(project_db, conversation.id) if not m.folded]
    # 折时库里是 5 条，留下最近两条（第 4、5 轮），第 6 轮是本轮刚落库的回答
    assert [m.turn_no for m in live] == [4, 5, 6]
    assert result.folded_turns == (1, 2, 3)


def test_消息没多到要折时一条也不折(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    candidate: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有两条还塞不下，那是模型窗口配小了，该报错而不是接着折。"""
    monkeypatch.setattr(engine.get_agent(DESIGNER), "context_budget", 10)
    conversation = start_project_talk(project_db)
    chat = ScriptedChat()

    result = send(project_db, session, project, conversation, "很长的一段" * 50, chat)

    assert result.folded_turns == ()
    assert not any(m.folded for m in engine.messages_of(project_db, conversation.id))


def test_摘要为空就停手不空转(
    project_db: Session,
    project: ProjectRef,
    session: Session,
    candidate: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型压不出摘要时继续折也压不下来，别在这儿转满上限。"""
    conversation = start_project_talk(project_db)
    chat = ScriptedChat()
    for i in range(1, 5):
        send(project_db, session, project, conversation, f"第{i}轮" * 30, chat)

    monkeypatch.setattr(engine.get_agent(DESIGNER), "context_budget", 100)
    chat.default = ""
    chat.replies = []
    calls_before = len(chat.calls)

    with pytest.raises(Exception, match="空回答"):
        send(project_db, session, project, conversation, "第五轮", chat)

    # 折叠只试了一次就收手，没有把 MAX_FOLD_ROUNDS 跑满
    assert len(chat.calls) - calls_before == 2


# --------------------------------------------------------------------------- #
# 草稿：未确认时磁盘无改动
# --------------------------------------------------------------------------- #


def test_未确认时磁盘一个字都不改(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    target = project.absolute("art-bible.md")
    before = target.read_text(encoding="utf-8")
    conversation = start_project_talk(project_db)

    send(project_db, session, project, conversation, "拟一版规范", ScriptedChat(DRAFT_REPLY))

    assert target.read_text(encoding="utf-8") == before
    draft = engine.drafts_of(project_db, conversation.id)[0]
    assert draft.target_path == "art-bible.md"
    assert draft.status == "pending"
    assert draft.based_on_hash == archive.file_hash(target)


def test_同一目标只留最新一份待确认草稿(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    """Agent 会反复重出全文，留着历史份只会让确认沉淀面对一堆同名草稿。"""
    conversation = start_project_talk(project_db)
    chat = ScriptedChat(DRAFT_REPLY, DRAFT_REPLY.replace("湿滑金属", "干燥砂岩"))

    send(project_db, session, project, conversation, "第一版", chat)
    send(project_db, session, project, conversation, "改一下", chat)

    pending = engine.drafts_of(project_db, conversation.id)
    assert len(pending) == 1
    assert "干燥砂岩" in pending[0].content
    superseded = engine.drafts_of(project_db, conversation.id, status="superseded")
    assert len(superseded) == 1


def test_角色会话的草稿归到角色目录(
    project_db: Session, project: ProjectRef, session: Session
) -> None:
    """Agent 只写文件名，不归位就一堆设定文档全落在项目根上。"""
    bind_text_model(session, WRITER)
    make_character(project_db)
    conversation = engine.start(
        project_db, agent_code=WRITER, target_kind="character", target_ref="c-赤瞳"
    )
    reply = "[草稿开始: 赤瞳角色设定.md]\n# 赤瞳\n[草稿结束]\n"

    send(project_db, session, project, conversation, "写设定", ScriptedChat(reply))

    draft = engine.drafts_of(project_db, conversation.id)[0]
    assert draft.target_path == "characters/赤瞳/赤瞳角色设定.md"


def test_草稿路径越不出项目目录(project_db: Session, project: ProjectRef) -> None:
    conversation = start_project_talk(project_db)

    with pytest.raises(LayoutError, match="越出了项目目录"):
        engine.resolve_draft_path(project_db, project, conversation, "../../.ssh/config")


def test_立项会话看得见项目配置现状(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    """看不见现值，改设定的会话只能整份重写；看不见键名，它会发明平台静默丢弃的键。"""
    conversation = start_project_talk(project_db)
    chat = ScriptedChat("知道了。")

    send(project_db, session, project, conversation, "把评审改严", chat)

    system = chat.system_of_last
    assert "项目配置现状" in system
    assert "review_mode" in system
    # 平台自己的账不摆进去，免得 Agent 以为这两个键也归它改
    assert '"code"' not in system


def test_角色会话不看项目配置(project_db: Session, project: ProjectRef, session: Session) -> None:
    """角色会话改不了项目配置，摆进去只是白占上下文预算。"""
    bind_text_model(session, WRITER)
    make_character(project_db)
    conversation = engine.start(
        project_db, agent_code=WRITER, target_kind="character", target_ref="c-赤瞳"
    )
    chat = ScriptedChat("知道了。")

    send(project_db, session, project, conversation, "写设定", chat)

    assert "项目配置现状" not in chat.system_of_last


def test_立项会话只写得了项目根上那两份(project_db: Session, project: ProjectRef) -> None:
    """路径在项目目录内不等于归这场会话管：立项 Agent 顺手改角色设定，是越权而不是帮忙。"""
    conversation = start_project_talk(project_db)

    assert engine.resolve_draft_path(project_db, project, conversation, "art-bible.md")
    assert engine.resolve_draft_path(project_db, project, conversation, layout.PROJECT_JSON)
    with pytest.raises(Conflict, match="不在它的职责范围内"):
        engine.resolve_draft_path(
            project_db, project, conversation, "characters/赤瞳/赤瞳角色设定.md"
        )


def test_角色会话改不了别的角色(project_db: Session, project: ProjectRef) -> None:
    """带目录的路径不再被归位，所以这里真的会写到别人头上，得有一道白名单拦着。"""
    make_character(project_db)
    conversation = engine.start(
        project_db, agent_code=WRITER, target_kind="character", target_ref="c-赤瞳"
    )

    assert engine.resolve_draft_path(project_db, project, conversation, "赤瞳角色设定.md")
    with pytest.raises(Conflict, match="不在它的职责范围内"):
        engine.resolve_draft_path(project_db, project, conversation, "characters/蓝羽/设定.md")
    with pytest.raises(Conflict, match="不在它的职责范围内"):
        engine.resolve_draft_path(
            project_db, project, conversation, "characters/赤瞳/../art-bible.md"
        )


def test_草稿里的空洞在确认之前就摆出来(project: ProjectRef) -> None:
    """art bible 缺的节会一路传进每一张图的 prompt，得在按下沉淀之前说。"""
    gaps = engine.draft_warnings(project, "art-bible.md", "# 规范\n\n## 1 视觉身份一句话\n\n冷。\n")
    assert any("色彩系统" in one for one in gaps)

    ignored = engine.draft_warnings(project, layout.PROJECT_JSON, '{"code": "别的"}')
    assert any("忽略" in one for one in ignored)

    # 角色设定文档没有这类结构约定，别硬凑提醒
    assert engine.draft_warnings(project, "characters/赤瞳/赤瞳角色设定.md", "# 赤瞳\n") == []


# --------------------------------------------------------------------------- #
# 确认沉淀
# --------------------------------------------------------------------------- #


def test_确认后定稿落盘旧版进tmp(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    target = project.absolute("art-bible.md")
    old = target.read_text(encoding="utf-8")
    conversation = start_project_talk(project_db)
    send(project_db, session, project, conversation, "拟一版", ScriptedChat(DRAFT_REPLY))

    result = engine.commit(project_db, project, conversation)

    assert "湿滑金属" in target.read_text(encoding="utf-8")
    assert len(result.archived) == 1
    retired = project.absolute(result.archived[0].previous_path or "")
    assert retired.parent.name == layout.TMP_DIR
    assert retired.read_text(encoding="utf-8") == old
    assert conversation.status == "active"
    assert engine.drafts_of(project_db, conversation.id, status="committed")


def test_沉淀时才把偏好写进长期记忆(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    """聊到一半的偏好可能下一轮就被否掉，提前写会让后续所有 Agent 都带着一条已反悔的要求。"""
    conversation = start_project_talk(project_db)
    send(project_db, session, project, conversation, "拟一版", ScriptedChat(DRAFT_REPLY))

    assert engine.enabled_memories(project_db) == []

    result = engine.commit(project_db, project, conversation)

    assert sorted(result.memories_added) == ["不要蒸汽朋克齿轮", "喜欢冷色调"]
    kinds = {(m.kind, m.content) for m in engine.enabled_memories(project_db)}
    assert ("taboo", "不要蒸汽朋克齿轮") in kinds


def test_同一条记忆重复出现只存一条(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    conversation = start_project_talk(project_db)
    chat = ScriptedChat(DRAFT_REPLY, "[项目记忆]\npreference: 喜欢冷色调 \n")
    send(project_db, session, project, conversation, "第一轮", chat)
    send(project_db, session, project, conversation, "第二轮", chat)

    engine.commit(project_db, project, conversation)

    contents = [m.content for m in engine.enabled_memories(project_db)]
    assert contents.count("喜欢冷色调") == 1


def test_沉淀记一条事件日志(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    conversation = start_project_talk(project_db)
    send(project_db, session, project, conversation, "拟一版", ScriptedChat(DRAFT_REPLY))

    engine.commit(project_db, project, conversation)

    events = list(project_db.scalars(select(TaskEvent).where(TaskEvent.task_id == conversation.id)))
    assert [e.event for e in events] == ["artifact_committed"]
    assert events[0].payload["target_path"] == "art-bible.md"


def test_只沉淀挑中的那几份(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    conversation = start_project_talk(project_db)
    reply = (
        DRAFT_REPLY
        + '\n[草稿开始: project.json]\n{"style": {"art_style": "赛博朋克"}}\n[草稿结束]\n'
    )
    send(project_db, session, project, conversation, "拟一版", ScriptedChat(reply))
    drafts = {d.target_path: d.id for d in engine.drafts_of(project_db, conversation.id)}

    result = engine.commit(project_db, project, conversation, draft_ids=[drafts["art-bible.md"]])

    assert [a.target_path for a in result.archived] == ["art-bible.md"]
    leftover = project_db.get(ArtifactDraft, drafts["project.json"])
    assert leftover is not None and leftover.status == "pending"


def test_没有草稿就不许沉淀(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    conversation = start_project_talk(project_db)
    send(project_db, session, project, conversation, "先聊聊", ScriptedChat("好的，先聊。"))

    with pytest.raises(Conflict, match="待确认"):
        engine.commit(project_db, project, conversation)


def test_挑了不属于这个会话的草稿就报错(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    conversation = start_project_talk(project_db)
    send(project_db, session, project, conversation, "拟一版", ScriptedChat(DRAFT_REPLY))

    with pytest.raises(NotFound):
        engine.commit(project_db, project, conversation, draft_ids=["不存在的草稿"])


def test_定稿被别处改过时沉淀失败且不落盘(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    conversation = start_project_talk(project_db)
    send(project_db, session, project, conversation, "拟一版", ScriptedChat(DRAFT_REPLY))
    target = project.absolute("art-bible.md")
    target.write_text("# 用户自己手改的\n", encoding="utf-8")

    with pytest.raises(Conflict, match="被改过"):
        engine.commit(project_db, project, conversation)

    assert target.read_text(encoding="utf-8") == "# 用户自己手改的\n"
    assert conversation.status == "active"  # 还能重新拟


def test_角色沉淀设定后回填spec_path但不动状态(
    project_db: Session, project: ProjectRef, session: Session
) -> None:
    """状态推进有门禁，沉淀一份文档不等于过审。"""
    bind_text_model(session, WRITER)
    character = make_character(project_db)
    conversation = engine.start(
        project_db, agent_code=WRITER, target_kind="character", target_ref=character.id
    )
    reply = "[草稿开始: 赤瞳角色设定.md]\n# 赤瞳\n[草稿结束]\n"
    send(project_db, session, project, conversation, "写设定", ScriptedChat(reply))

    engine.commit(project_db, project, conversation)

    assert character.spec_path == "characters/赤瞳/赤瞳角色设定.md"
    assert character.state == "S0_spec_drafting"
    assert character.gate_spec_confirmed_at is None


# --------------------------------------------------------------------------- #
# 丢弃
# --------------------------------------------------------------------------- #


def test_丢弃只标草稿弃用消息全留着(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    conversation = start_project_talk(project_db)
    send(project_db, session, project, conversation, "拟一版", ScriptedChat(DRAFT_REPLY))

    count = engine.discard(project_db, conversation)

    assert count == 1
    assert conversation.status == "active"
    assert len(engine.messages_of(project_db, conversation.id)) == 2
    assert engine.drafts_of(project_db, conversation.id) == []
    assert "湿滑金属" not in project.absolute("art-bible.md").read_text(encoding="utf-8")


def test_沉淀过的会话还能接着聊并再沉淀一版(
    project_db: Session, project: ProjectRef, session: Session, candidate: None
) -> None:
    """沉淀是「这一版落盘」，不是「这场聊完了」：接着改还得在同一场里，上下文才不用重讲。"""
    target = project.absolute("art-bible.md")
    conversation = start_project_talk(project_db)
    send(project_db, session, project, conversation, "拟一版", ScriptedChat(DRAFT_REPLY))
    engine.commit(project_db, project, conversation)

    second = DRAFT_REPLY.replace("湿滑金属", "哑光陶土")
    send(project_db, session, project, conversation, "再改一版", ScriptedChat(second))
    engine.commit(project_db, project, conversation)

    assert "哑光陶土" in target.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 长期记忆
# --------------------------------------------------------------------------- #


def test_手写记忆与agent沉淀的走同一套去重(project_db: Session, project: ProjectRef) -> None:
    assert engine.write_memory(project_db, "preference", "喜欢冷色调") is not None
    assert engine.write_memory(project_db, "preference", " 喜欢冷色调  ") is None
    # 换个类别就是另一条
    assert engine.write_memory(project_db, "fact", "喜欢冷色调") is not None

    project_db.commit()
    assert len(list(project_db.scalars(select(ProjectMemory)))) == 2


def test_停用的记忆不再注入(project_db: Session, project: ProjectRef) -> None:
    row = engine.write_memory(project_db, "preference", "喜欢冷色调")
    assert row is not None
    row.enabled = False
    project_db.commit()

    assert engine.enabled_memories(project_db) == []
