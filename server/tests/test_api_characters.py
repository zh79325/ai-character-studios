"""角色接口的 HTTP 全流程：建角色、评审、门禁 1、状态推进。

这一层要钉的是**门禁 1 真的拦得住**：设定没人工确认，`POST /advance` 就得 409。所以用例走
的是完整那条路——开设定会话、聊出草稿、确认沉淀、评审、人工放行，中间任何一步没做都得被
挡下来。模型走假实现，验的是接口契约与拦阻，不是模型会不会好好说话。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from atelier.agents import render, review
from atelier.assets import characters, projects
from atelier.assets.projects import ProjectRef
from atelier.providers import image_gen, text_chat
from tests.conftest import ScriptedChat, bind_image_model, bind_text_model
from tests.test_characters import PARTIAL_BIBLE
from tests.test_render import CARD, ScriptedDraw

WRITER = "spec_writer"

SPEC_REPLY = """给你一版设定。

[草稿开始: 赤瞳角色设定.md]
# 赤瞳
双尾、红瞳、三指利爪，栖息在废弃电厂。
[草稿结束]
"""

APPROVE_REPLY = """SPEC-CHECK: APPROVE

### 缺失维度
无

### 硬性约束清单
- 尾巴 = 2 条，彼此分离
- 眼睛 = 红色发光
"""

REJECT_REPLY = """SPEC-CHECK: REJECT

### 缺失维度
- 环境设定

### 硬性约束清单
- 尾巴 = 2 条
"""


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch) -> ScriptedChat:
    """把文本驱动换成脚本。每个用例自己往 `replies` 里排队要的回答。"""
    scripted = ScriptedChat()
    monkeypatch.setattr(text_chat, "complete", scripted)
    return scripted


@pytest.fixture
def candidates(session: Session) -> None:
    """写手与评审各一个候选，落在不同 provider 上免得撞主键。"""
    bind_text_model(session, WRITER)
    bind_text_model(session, review.REVIEWER, code="ark")
    bind_text_model(session, render.SMITH, code="smith")
    bind_image_model(session, render.PAINTER, code="ark-image")


@pytest.fixture
def draw(monkeypatch: pytest.MonkeyPatch) -> ScriptedDraw:
    """把生图驱动换成假实现，接口层验的是契约与拦阻。"""
    scripted = ScriptedDraw()
    monkeypatch.setattr(image_gen, "generate", scripted)
    return scripted


@pytest.fixture
def ready(project: ProjectRef) -> ProjectRef:
    """项目已经聊出过视觉规范，够开工建角色了。"""
    projects.write_art_bible(project, PARTIAL_BIBLE)
    return project


def create(client: TestClient, name: str = "赤瞳") -> dict[str, object]:
    response = client.post("/api/characters", json={"name": name})
    assert response.status_code == 201, response.text
    return dict(response.json())


def settle_spec(client: TestClient, character_id: str, chat: ScriptedChat) -> None:
    """走完设定会话那条路：聊出草稿、确认沉淀，让 `spec_path` 落到库行上。

    不直接改库：门禁确认的就是「用户按过确认沉淀」这件事，绕过它测出来的通过没有意义。
    """
    chat.replies.append(SPEC_REPLY)
    opened = client.post(
        "/api/conversations",
        json={"agent_code": WRITER, "target_kind": "character", "target_ref": character_id},
    )
    assert opened.status_code == 201, opened.text
    cid = opened.json()["conversation"]["id"]
    turn = client.post(f"/api/conversations/{cid}/messages", json={"content": "先出一版"})
    assert turn.status_code == 200, turn.text
    committed = client.post(f"/api/conversations/{cid}/commit", json={})
    assert committed.status_code == 200, committed.text


# --------------------------------------------------------------------------- #
# 建角色
# --------------------------------------------------------------------------- #


def test_项目还没写视觉规范就不给建角色(
    client: TestClient, project: ProjectRef, candidates: None
) -> None:
    """art bible 是设定的风格锚点，拿模板原样当锚点等于没有锚点。"""
    response = client.post("/api/characters", json={"name": "赤瞳"})

    assert response.status_code == 409
    assert "视觉规范" in response.json()["detail"]


def test_建角色给出状态与人话说法(client: TestClient, ready: ProjectRef, candidates: None) -> None:
    body = create(client)

    assert body["state"] == characters.SPEC_DRAFTING
    assert body["state_label"] == "设定对焦中"
    assert body["dir_name"] == "characters/赤瞳"
    assert body["hard_constraints"] == []
    assert body["gate_spec_confirmed_at"] is None


def test_同名角色只能有一个(client: TestClient, ready: ProjectRef, candidates: None) -> None:
    create(client)

    again = client.post("/api/characters", json={"name": "赤瞳"})

    assert again.status_code == 409


def test_不存在的角色是404(client: TestClient, ready: ProjectRef) -> None:
    assert client.get("/api/characters/nope").status_code == 404


def test_列表与详情给的是同一份字段(
    client: TestClient, ready: ProjectRef, candidates: None
) -> None:
    """两处各拼一遍的话，加字段时总会只加到其中一边。"""
    created = create(client)

    listed = client.get("/api/projects/current/characters")
    detail = client.get(f"/api/characters/{created['id']}")

    assert listed.status_code == 200
    assert listed.json() == [detail.json()]


# --------------------------------------------------------------------------- #
# 评审
# --------------------------------------------------------------------------- #


def test_评审给出裁决与约束清单(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    character = create(client)
    settle_spec(client, str(character["id"]), chat)
    chat.replies.append(APPROVE_REPLY)

    response = client.post(f"/api/characters/{character['id']}/review", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] == "APPROVE"
    assert body["approved"] is True
    assert body["manual"] is False
    assert body["constraints"] == [
        {"item": "尾巴", "value": "2 条，彼此分离"},
        {"item": "眼睛", "value": "红色发光"},
    ]
    assert body["text"] == APPROVE_REPLY.strip()


def test_通过了也还是要人按一下(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    """APPROVE 只表示审校没发现问题，放行是人的事。"""
    character = create(client)
    settle_spec(client, str(character["id"]), chat)
    chat.replies.append(APPROVE_REPLY)
    client.post(f"/api/characters/{character['id']}/review", json={})

    after = client.get(f"/api/characters/{character['id']}").json()

    assert after["state"] == characters.SPEC_DRAFTING
    assert after["gate_spec_confirmed_at"] is None
    assert [one["item"] for one in after["hard_constraints"]] == ["尾巴", "眼睛"]


def test_一个字都没有时评审说不出话(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    character = create(client)

    response = client.post(f"/api/characters/{character['id']}/review", json={})

    assert response.status_code == 409
    assert "还没有设定内容可审" in response.json()["detail"]


def test_裁决全文留在事件时间线里(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    """日后要回答「这份设定当时凭什么过的」，只有这条线答得上。"""
    character = create(client)
    settle_spec(client, str(character["id"]), chat)
    chat.replies.append(REJECT_REPLY)
    client.post(f"/api/characters/{character['id']}/review", json={})

    events = client.get(f"/api/characters/{character['id']}/events").json()

    reviewed = [one for one in events if one["event"] == "spec_reviewed"]
    assert reviewed[-1]["message"] == REJECT_REPLY.strip()
    assert reviewed[-1]["level"] == "warning"
    assert [one["seq"] for one in events] == list(range(1, len(events) + 1))


def test_带上会话才会驳回后自动重生(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    """重生得有个会话承载新的一轮，不然这几轮对话跟用户自己那场对不上号。"""
    character = create(client)
    opened = client.post(
        "/api/conversations",
        json={"agent_code": WRITER, "target_kind": "character", "target_ref": character["id"]},
    )
    cid = opened.json()["conversation"]["id"]
    chat.replies.append(SPEC_REPLY)
    client.post(f"/api/conversations/{cid}/messages", json={"content": "先出一版"})
    chat.replies.extend([REJECT_REPLY, SPEC_REPLY, APPROVE_REPLY])

    response = client.post(
        f"/api/characters/{character['id']}/review", json={"conversation_id": cid}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] == "APPROVE"
    assert body["regenerated"] == 1
    assert body["attempt"] == 2


def test_指了一个不存在的会话是404(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    character = create(client)

    response = client.post(
        f"/api/characters/{character['id']}/review", json={"conversation_id": "nope"}
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 门禁 1
# --------------------------------------------------------------------------- #


def test_没沉淀过设定就确认不了(client: TestClient, ready: ProjectRef, candidates: None) -> None:
    """门禁确认的是磁盘上那一份；库里躺着的草稿不算。"""
    character = create(client)

    response = client.post(f"/api/characters/{character['id']}/spec/confirm", json={})

    assert response.status_code == 409
    assert "确认沉淀" in response.json()["detail"]


def test_人工确认后进设定已确认(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    character = create(client)
    settle_spec(client, str(character["id"]), chat)

    response = client.post(
        f"/api/characters/{character['id']}/spec/confirm", json={"note": "看过了，可以"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == characters.SPEC_CONFIRMED
    assert body["state_label"] == "设定已确认"
    assert body["gate_spec_confirmed_at"] is not None
    events = client.get(f"/api/characters/{character['id']}/events").json()
    gate = [one for one in events if one["event"] == "gate_spec_confirmed"]
    assert gate[-1]["message"] == "看过了，可以"


def test_确认过一次就不再确认(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    character = create(client)
    settle_spec(client, str(character["id"]), chat)
    client.post(f"/api/characters/{character['id']}/spec/confirm", json={})

    again = client.post(f"/api/characters/{character['id']}/spec/confirm", json={})

    assert again.status_code == 409


def test_驳回不动状态但留下理由(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    """驳回不是一个新阶段，是「这一步还没过」。"""
    character = create(client)
    settle_spec(client, str(character["id"]), chat)

    response = client.post(
        f"/api/characters/{character['id']}/spec/reject", json={"note": "环境设定还没写"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == characters.SPEC_DRAFTING
    events = client.get(f"/api/characters/{character['id']}/events").json()
    assert events[-1]["event"] == "gate_spec_rejected"
    assert events[-1]["message"] == "环境设定还没写"


def test_驳回得写清哪里不行(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    character = create(client)

    response = client.post(f"/api/characters/{character['id']}/spec/reject", json={"note": "  "})

    assert response.status_code == 409


# --------------------------------------------------------------------------- #
# 门禁 1 之后：没确认就走不下去
# --------------------------------------------------------------------------- #


def test_设定没确认后续步骤一律拦住(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    """这是门禁 1 的验收点：没人按过确认，渲染图那一步就进不去。"""
    character = create(client)
    settle_spec(client, str(character["id"]), chat)
    chat.replies.append(APPROVE_REPLY)
    client.post(f"/api/characters/{character['id']}/review", json={})

    response = client.post(
        f"/api/characters/{character['id']}/advance", json={"state": "S2_render_generated"}
    )

    assert response.status_code == 409
    assert "设定对焦中" in response.json()["detail"]


def test_确认之后才能推到下一步(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    character = create(client)
    settle_spec(client, str(character["id"]), chat)
    client.post(f"/api/characters/{character['id']}/spec/confirm", json={})

    response = client.post(
        f"/api/characters/{character['id']}/advance", json={"state": "S2_render_generated"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "S2_render_generated"


def test_不许跳级(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    character = create(client)
    settle_spec(client, str(character["id"]), chat)
    client.post(f"/api/characters/{character['id']}/spec/confirm", json={})

    response = client.post(
        f"/api/characters/{character['id']}/advance", json={"state": "S4_views_generated"}
    )

    assert response.status_code == 409
    assert "中间还有步骤没做" in response.json()["detail"]


def test_不认识的状态不当成还没开始(
    client: TestClient, ready: ProjectRef, candidates: None
) -> None:
    character = create(client)

    response = client.post(f"/api/characters/{character['id']}/advance", json={"state": "S99"})

    assert response.status_code == 409


# --------------------------------------------------------------------------- #
# 渲染图与门禁 2
# --------------------------------------------------------------------------- #


def confirmed(client: TestClient, chat: ScriptedChat) -> dict[str, object]:
    """设定已经过了门禁 1 的角色，够开工出渲染图。"""
    character = create(client)
    settle_spec(client, str(character["id"]), chat)
    passed = client.post(f"/api/characters/{character['id']}/spec/confirm", json={})
    assert passed.status_code == 200, passed.text
    return character


def test_设定没确认就生不了图(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    """这是门禁 1 在 HTTP 层的另一个出口：不能绕过 advance 直接生图。"""
    character = create(client)
    settle_spec(client, str(character["id"]), chat)

    response = client.post(f"/api/characters/{character['id']}/render", json={})

    assert response.status_code == 409
    assert "才能生成渲染图" in response.json()["detail"]
    assert draw.calls == []


def test_卡片可以先看不生图(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    """prompt 少一截层序，用户在图上只能看出「不对」而看不出「哪里不对」。"""
    character = confirmed(client, chat)
    chat.replies.append(CARD)

    response = client.post(f"/api/characters/{character['id']}/asset-spec", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == "ASSET-DEMO-001"
    assert body["size"] == "2048x2048"
    assert body["constraints"] == ["双尾数量=2"]
    assert body["card"].startswith("ASSET-DEMO-001")
    assert draw.calls == []


def test_生图后候选里列得出来(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = confirmed(client, chat)
    chat.replies.append(CARD)

    response = client.post(f"/api/characters/{character['id']}/render", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert "/tmp/" in body["file_path"]
    assert body["spec"]["code"] == "ASSET-DEMO-001"
    assert body["params"]["model"]
    listed = client.get(f"/api/characters/{character['id']}/renders").json()
    assert [one["id"] for one in listed] == [body["generation_id"]]
    assert listed[0]["is_final"] is False


def test_图本体按原格式发出去(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    """一张 2048 的 png 动辄几 MB，转 base64 塞进 JSON 再膨 33%。"""
    character = confirmed(client, chat)
    chat.replies.append(CARD)
    rendered = client.post(f"/api/characters/{character['id']}/render", json={}).json()

    response = client.get(
        f"/api/characters/{character['id']}/renders/{rendered['generation_id']}/image"
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == draw.data


def test_别人的产物读不到(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = confirmed(client, chat)
    chat.replies.append(CARD)
    rendered = client.post(f"/api/characters/{character['id']}/render", json={}).json()
    other = create(client, "青瞳")

    response = client.get(
        f"/api/characters/{other['id']}/renders/{rendered['generation_id']}/image"
    )

    assert response.status_code == 404


def test_采用得指名哪一张(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    """默认采用「最新一张」在用户连生了几张之后就不是他指的那一张了。"""
    character = confirmed(client, chat)
    chat.replies.append(CARD)
    client.post(f"/api/characters/{character['id']}/render", json={})

    response = client.post(f"/api/characters/{character['id']}/render/confirm", json={})

    assert response.status_code == 422


def test_采用之后进渲染图已确认(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = confirmed(client, chat)
    chat.replies.append(CARD)
    rendered = client.post(f"/api/characters/{character['id']}/render", json={}).json()

    response = client.post(
        f"/api/characters/{character['id']}/render/confirm",
        json={"generation_id": rendered["generation_id"], "note": "就这张"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == characters.RENDER_CONFIRMED
    assert body["state_label"] == "渲染图已定稿"
    assert body["render_path"] == "characters/赤瞳/images/character_赤瞳_渲染图.png"
    assert body["gate_render_confirmed_at"] is not None
    listed = client.get(f"/api/characters/{character['id']}/renders").json()
    assert listed[0]["is_final"] is True
    assert listed[0]["file_path"] == body["render_path"]


def test_不存在的产物采用不了(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = confirmed(client, chat)
    chat.replies.append(CARD)
    client.post(f"/api/characters/{character['id']}/render", json={})

    response = client.post(
        f"/api/characters/{character['id']}/render/confirm", json={"generation_id": "nope"}
    )

    assert response.status_code == 404


def test_驳回渲染图时状态停在S2(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = confirmed(client, chat)
    chat.replies.append(CARD)
    client.post(f"/api/characters/{character['id']}/render", json={})

    response = client.post(
        f"/api/characters/{character['id']}/render/reject", json={"note": "尾巴粘在一起了"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == characters.RENDER_GENERATED
    assert response.json()["render_path"] is None
    events = client.get(f"/api/characters/{character['id']}/events").json()
    assert events[-1]["event"] == "gate_render_rejected"
    assert events[-1]["message"] == "尾巴粘在一起了"
