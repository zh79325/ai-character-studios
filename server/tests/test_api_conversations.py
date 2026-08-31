"""会话接口的 HTTP 全流程：开会话、发一轮、看 diff、确认沉淀、丢弃、长期记忆。

模型走假实现（`text_chat.complete` 被换掉），所以这里验的是接口契约：状态码、出参字段、
草稿过期标记、SSE 增量。`POST /messages` 是同步的，所以「先订流再发消息」这条时序也要真
的跑一遍，不能只测缓冲对象。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from atelier.agents import conversation as engine
from atelier.agents.stream_bus import BUS, ERROR
from atelier.api import conversations as api
from atelier.assets.projects import ProjectRef
from atelier.db.project_models import Message
from atelier.providers import text_chat
from tests.conftest import ScriptedChat, bind_text_model

DESIGNER = "game_designer"

DRAFT_REPLY = """给你一版。

[对焦进度]
已定：题材是赛博朋克
待定：面数预算
下一步：确认平台

[草稿开始: art-bible.md]
# 视觉规范
冷光下的湿滑金属。
[草稿结束]

[项目记忆]
preference: 喜欢冷色调
"""


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """增量是轮询广播缓冲推的，把间隔压下来免得用例白等。"""
    monkeypatch.setattr(api, "POLL_SECONDS", 0.01)


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch) -> ScriptedChat:
    """把文本驱动换成脚本，单测不打网络。"""
    scripted = ScriptedChat(DRAFT_REPLY)
    monkeypatch.setattr(text_chat, "complete", scripted)
    return scripted


@pytest.fixture
def talk(
    client: TestClient, project: ProjectRef, session: Session, chat: ScriptedChat
) -> Iterator[str]:
    """一个已开好、已绑好候选的项目会话，返回它的 id。"""
    bind_text_model(session, DESIGNER)
    response = client.post(
        "/api/conversations", json={"agent_code": DESIGNER, "target_kind": "project"}
    )
    assert response.status_code == 201
    yield response.json()["conversation"]["id"]


def send(client: TestClient, cid: str, text: str, *, stream: bool = False) -> dict[str, object]:
    response = client.post(
        f"/api/conversations/{cid}/messages", json={"content": text, "stream": stream}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


class Frame(NamedTuple):
    """一帧 SSE。带着 `id` 才能照真实游标重连，靠数帧数推游标一遇到多行增量就算歪了。"""

    id: str
    event: str
    data: str


def parse_sse(body: str) -> list[Frame]:
    """把整段响应拆回一帧一帧。带换行的增量会被拆成多行 `data:`，要拼回去。"""
    frames: list[Frame] = []
    event = seq = ""
    data: list[str] = []
    for line in body.splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("id:"):
            seq = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:") :].lstrip())
        elif not line and data:  # 空行收一帧
            frames.append(Frame(seq, event, "\n".join(data)))
            event = seq = ""
            data = []
    return frames


def read_stream(
    client: TestClient, cid: str, *, headers: dict[str, str] | None = None
) -> list[Frame]:
    """订一次流并等它自己收。

    TestClient 不做真正的增量读（拿到的是整段响应），所以这里靠的是接口本身会在一轮
    有了结果后收流——没这个收尾，这一句就是死等。
    """
    body = client.get(f"/api/conversations/{cid}/stream", headers=headers).text
    return [f for f in parse_sse(body) if f.event != "ready"]


def _seq(frame: Frame) -> int:
    """帧 id 是 `会话id:序号`。"""
    return int(frame.id.rpartition(":")[2])


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #


def test_开会话返回空的详情(client: TestClient, project: ProjectRef, session: Session) -> None:
    response = client.post(
        "/api/conversations",
        json={"agent_code": DESIGNER, "target_kind": "project", "title": "立项"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["conversation"]["title"] == "立项"
    assert body["messages"] == []
    assert body["drafts"] == []
    assert body["artifact_path"] == "art-bible.md"


def test_非会话型agent开不了(client: TestClient, project: ProjectRef) -> None:
    response = client.post(
        "/api/conversations", json={"agent_code": "prompt_smith", "target_kind": "project"}
    )

    assert response.status_code == 409


def test_不存在的会话是404(client: TestClient, project: ProjectRef) -> None:
    assert client.get("/api/conversations/nope").status_code == 404


def test_列表按目标过滤(client: TestClient, project: ProjectRef) -> None:
    client.post("/api/conversations", json={"agent_code": DESIGNER, "target_kind": "project"})

    listed = client.get("/api/conversations", params={"target_kind": "project"}).json()

    assert len(listed) == 1
    assert listed[0]["message_count"] == 0
    assert client.get("/api/conversations", params={"target_kind": "character"}).json() == []


def ensure(client: TestClient) -> dict[str, object]:
    """拿项目当下该聊的那场会话。立项页进来就调这一口。"""
    response = client.post(
        "/api/conversations/ensure", json={"agent_code": DESIGNER, "target_kind": "project"}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_ensure接着还开着的那场聊(client: TestClient, project: ProjectRef) -> None:
    """刷几次页不该攒出几场空会话。"""
    first = ensure(client)

    again = ensure(client)

    assert again["conversation"]["id"] == first["conversation"]["id"]  # type: ignore[index]
    assert len(client.get("/api/conversations").json()) == 1


def test_ensure沉淀过也还是同一场(client: TestClient, talk: str) -> None:
    """沉淀只是把草稿写进定稿位，聊到哪儿还在这场里，另起一场等于把上下文丢掉。"""
    send(client, talk, "拟一版")
    assert client.post(f"/api/conversations/{talk}/commit", json={}).status_code == 200

    body = ensure(client)

    assert body["conversation"]["id"] == talk  # type: ignore[index]
    assert len(client.get("/api/conversations").json()) == 1


def test_开场提示报出项目现状(client: TestClient, project: ProjectRef) -> None:
    """已立项的项目要先总结一遍手里有什么，用户才知道该不该接着对焦。"""
    body = ensure(client)

    assert project.name in str(body["briefing"])
    assert "art-bible.md" in str(body["briefing"])
    assert body["briefing_blank"] is False


def test_白纸项目直接请用户说想法(client: TestClient, projects_root: Path, tmp_path: Path) -> None:
    """立项一开头没任何可总结的东西，那就只给一句号召，前端拿 `briefing_blank` 铺成大字。"""
    created = client.post("/api/projects/bootstrap", json={"dir_path": str(tmp_path / "新项目")})
    assert created.status_code == 201, created.text

    body = ensure(client)

    assert body["briefing"] == engine.BRIEFING_BLANK
    assert body["briefing_blank"] is True


# --------------------------------------------------------------------------- #
# 一轮对话
# --------------------------------------------------------------------------- #


def test_发一轮拿到回答与草稿(client: TestClient, talk: str) -> None:
    body = send(client, talk, "做个赛博朋克项目")

    assert body["turn_no"] == 2
    assert len(body["draft_ids"]) == 1
    assert body["provider_label"] == "bailian/qwen-plus"
    assert body["context_tokens"] > 0

    detail = client.get(f"/api/conversations/{talk}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["memory"]["decisions"] == ["题材是赛博朋克"]
    assert detail["drafts"][0]["target_path"] == "art-bible.md"
    assert detail["drafts"][0]["stale"] is False


CHOICE_REPLY = """还有几处等你拍。

[待选项]
- 项: 参考作品锚点 / 选项: 银翼杀手 | 攻壳机动队 / 多选: 是 / 推荐: 银翼杀手 | 攻壳机动队
- 项: 面数预算 / 选项: 8k | 15k / 多选: 否 / 推荐: 15k
"""


def test_待选项带着单选多选与推荐出去(client: TestClient, talk: str, chat: ScriptedChat) -> None:
    """前端靠 `multiple` 决定摆单选还是多选，靠 `recommended` 预选；两个字段必须原样到线上。"""
    chat.replies.append(CHOICE_REPLY)
    send(client, talk, "拟一版")
    send(client, talk, "接着说")

    detail = client.get(f"/api/conversations/{talk}").json()

    assert detail["choices"] == [
        {
            "item": "参考作品锚点",
            "options": ["银翼杀手", "攻壳机动队"],
            "recommended": ["银翼杀手", "攻壳机动队"],
            "multiple": True,
        },
        {
            "item": "面数预算",
            "options": ["8k", "15k"],
            "recommended": ["15k"],
            "multiple": False,
        },
    ]


def test_空内容不发(client: TestClient, talk: str) -> None:
    response = client.post(f"/api/conversations/{talk}/messages", json={"content": "   "})

    assert response.status_code == 409


def test_草稿的基线过期时提前标出来(client: TestClient, talk: str, project: ProjectRef) -> None:
    """等点了沉淀才收到 409 太晚了，前端要能先显示「已过期」。"""
    send(client, talk, "拟一版")
    project.absolute("art-bible.md").write_text("# 用户手改了\n", encoding="utf-8")

    detail = client.get(f"/api/conversations/{talk}").json()

    assert detail["drafts"][0]["stale"] is True


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #


def test_diff给两份全文交前端渲染(client: TestClient, talk: str, project: ProjectRef) -> None:
    """算法与展示形式都是前端的事，后端算成文本传过去反而限制了它。"""
    draft_id = send(client, talk, "拟一版")["draft_ids"][0]  # type: ignore[index]

    body = client.get(f"/api/conversations/{talk}/drafts/{draft_id}/diff").json()

    assert body["target_path"] == "art-bible.md"
    assert body["current"] == project.absolute("art-bible.md").read_text(encoding="utf-8")
    assert "湿滑金属" in body["draft"]


def test_diff顺手带上没写完的地方(client: TestClient, talk: str) -> None:
    """这一屏是用户按下沉淀前看的最后一眼，缺的节得就在这里说，往后没机会了。"""
    draft_id = send(client, talk, "拟一版")["draft_ids"][0]  # type: ignore[index]

    body = client.get(f"/api/conversations/{talk}/drafts/{draft_id}/diff").json()

    # 剧本里的草稿只写了一句话，六节里的其余几节都还空着
    assert len(body["warnings"]) > 0
    assert any("风格禁止项" in one for one in body["warnings"])


def test_别的会话的草稿看不到(client: TestClient, talk: str) -> None:
    send(client, talk, "拟一版")
    other = client.post(
        "/api/conversations", json={"agent_code": DESIGNER, "target_kind": "project"}
    ).json()["conversation"]["id"]
    draft_id = client.get(f"/api/conversations/{talk}").json()["drafts"][0]["id"]

    assert client.get(f"/api/conversations/{other}/drafts/{draft_id}/diff").status_code == 404


# --------------------------------------------------------------------------- #
# 沉淀与丢弃
# --------------------------------------------------------------------------- #


def test_确认沉淀后定稿落盘(client: TestClient, talk: str, project: ProjectRef) -> None:
    send(client, talk, "拟一版")

    body = client.post(f"/api/conversations/{talk}/commit", json={}).json()

    assert "湿滑金属" in project.absolute("art-bible.md").read_text(encoding="utf-8")
    assert body["archived"][0]["previous_path"].startswith("tmp/")
    assert body["memories_added"] == ["喜欢冷色调"]
    # 沉淀不收口会话：接着聊下一版还得在同一场里
    assert client.get(f"/api/conversations/{talk}").json()["conversation"]["status"] == "active"


def test_基线过期时沉淀返回409(client: TestClient, talk: str, project: ProjectRef) -> None:
    send(client, talk, "拟一版")
    project.absolute("art-bible.md").write_text("# 用户手改了\n", encoding="utf-8")

    response = client.post(f"/api/conversations/{talk}/commit", json={})

    assert response.status_code == 409
    assert project.absolute("art-bible.md").read_text(encoding="utf-8") == "# 用户手改了\n"


def test_丢弃后磁盘没动(client: TestClient, talk: str, project: ProjectRef) -> None:
    before = project.absolute("art-bible.md").read_text(encoding="utf-8")
    send(client, talk, "拟一版")

    body = client.post(f"/api/conversations/{talk}/discard").json()

    assert body["discarded"] == 1
    assert project.absolute("art-bible.md").read_text(encoding="utf-8") == before
    assert client.get(f"/api/conversations/{talk}").json()["conversation"]["status"] == "active"


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #


def test_订阅流能收到开流之后的增量(client: TestClient, talk: str) -> None:
    """先订流再发消息：这条流只是让字一个个出现，会话的真相始终在库里。

    发消息交给定时线程，时序跟真实使用一致：订流在前，增量在后。
    """
    failures: list[BaseException] = []

    def send_later() -> None:
        try:
            send(client, talk, "拟一版", stream=True)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
            BUS.publish(talk, ERROR, "发消息没成")  # 不发就把读整段的那头挂死了

    timer = threading.Timer(0.1, send_later)
    timer.start()
    events = read_stream(client, talk)
    timer.join()

    assert not failures
    kinds = [f.event for f in events]
    assert "delta" in kinds
    assert kinds[-1] == "turn"  # 推到这条就收流，不挂着等客户端断开
    assert any("湿滑金属" in f.data for f in events if f.event == "delta")
    assert json.loads(events[-1].data)["turn_no"] == 2


def test_一轮失败也要告诉订流的那头(client: TestClient, talk: str, chat: ScriptedChat) -> None:
    """模型炸了而流里什么都没有，前端就只能干等到超时。"""
    chat.replies = ["   "]  # 空回答会被引擎当失败

    response = client.post(f"/api/conversations/{talk}/messages", json={"content": "拟一版"})

    assert response.status_code >= 400
    last = read_stream(client, talk)[-1]
    assert last.event == "error"
    assert "空回答" in last.data


def test_重连时不重复推已看过的增量(client: TestClient, talk: str) -> None:
    """浏览器自动重连只带 `Last-Event-ID`，不认它就会把整轮回答再刷一遍。"""
    send(client, talk, "拟一版", stream=True)

    whole = read_stream(client, talk)
    assert len(whole) > 1

    resumed = read_stream(client, talk, headers={"Last-Event-ID": whole[-2].id})
    assert [f.event for f in resumed] == ["turn"]


def test_新一轮不会把上一轮重放一遍(client: TestClient, talk: str) -> None:
    """缓冲里留着上一轮的话，新订上来的流会被那段末尾的 turn 当场收掉，这一轮就只剩转圈。"""
    send(client, talk, "拟一版", stream=True)
    first = read_stream(client, talk)

    send(client, talk, "再拟一版", stream=True)

    again = read_stream(client, talk)
    assert [json.loads(f.data)["turn_no"] for f in again if f.event == "turn"] == [4]
    # 序号接着数，但上一轮的那几帧已经不在缓冲里
    assert min(_seq(f) for f in again) > max(_seq(f) for f in first)


def test_不存在的会话订不了流(client: TestClient, project: ProjectRef) -> None:
    assert client.get("/api/conversations/nope/stream").status_code == 404


# --------------------------------------------------------------------------- #
# 中断
# --------------------------------------------------------------------------- #


def test_答完的消息带着已完成的状态(client: TestClient, talk: str) -> None:
    send(client, talk, "拟一版")

    rows = client.get(f"/api/conversations/{talk}").json()["messages"]
    assert [one["status"] for one in rows] == ["done", "done"]


def test_没在跑的时候中断也算成(client: TestClient, talk: str) -> None:
    """点下去那一瞬刚好回完也不算错，报个错只会让用户以为自己把事情搞砸了。"""
    response = client.post(f"/api/conversations/{talk}/interrupt")

    assert response.status_code == 200
    assert response.json() == {"conversation_id": talk, "interrupted": False}


def test_重启卡住的那条正在想能被中断清掉(
    client: TestClient, talk: str, project_db: Session
) -> None:
    """进程换了一个，推理早没了，库里那条 `thinking` 却还挂着——中断这一口就是它唯一的出路。"""
    project_db.add(
        Message(conversation_id=talk, turn_no=1, role="assistant", content="", status="thinking")
    )
    project_db.commit()

    assert client.post(f"/api/conversations/{talk}/interrupt").json()["interrupted"] is True

    rows = client.get(f"/api/conversations/{talk}").json()["messages"]
    assert [one["status"] for one in rows] == ["cancelled"]


# --------------------------------------------------------------------------- #
# 项目长期记忆
# --------------------------------------------------------------------------- #


def test_手写记忆的增改停删(client: TestClient, project: ProjectRef) -> None:
    created = client.post("/api/memory", json={"kind": "taboo", "content": "不要齿轮"})
    assert created.status_code == 201
    memory_id = created.json()["id"]

    # 同一条再写一次是冲突，不是静默去重
    assert (
        client.post("/api/memory", json={"kind": "taboo", "content": "不要齿轮 "}).status_code
        == 409
    )

    patched = client.patch(f"/api/memory/{memory_id}", json={"enabled": False}).json()
    assert patched["enabled"] is False

    assert client.delete(f"/api/memory/{memory_id}").status_code == 204
    assert client.get("/api/memory").json() == []


def test_停用的记忆还在列表里(client: TestClient, project: ProjectRef) -> None:
    """停用是让它不再注入，不是删掉——列表里要看得见才能改回来。"""
    created = client.post("/api/memory", json={"kind": "preference", "content": "冷色调"}).json()
    client.patch(f"/api/memory/{created['id']}", json={"enabled": False})

    listed = client.get("/api/memory").json()

    assert len(listed) == 1
    assert listed[0]["enabled"] is False


def test_改内容后去重的键跟着变(
    client: TestClient, project: ProjectRef, project_db: Session
) -> None:
    created = client.post("/api/memory", json={"kind": "fact", "content": "平台是 PC"}).json()

    client.patch(f"/api/memory/{created['id']}", json={"content": "平台是 PC 与 Switch"})

    project_db.expire_all()
    assert engine.write_memory(project_db, "fact", "平台是 PC 与 Switch") is None
    assert engine.write_memory(project_db, "fact", "平台是 PC") is not None


def test_改不存在的记忆是404(client: TestClient, project: ProjectRef) -> None:
    assert client.patch("/api/memory/nope", json={"enabled": False}).status_code == 404
    assert client.delete("/api/memory/nope").status_code == 404
