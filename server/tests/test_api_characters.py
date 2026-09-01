"""角色接口的 HTTP 全流程：建角色、评审、门禁 1、状态推进。

这一层要钉的是**门禁 1 真的拦得住**：设定没人工确认，`POST /advance` 就得 409。所以用例走
的是完整那条路——开设定会话、聊出草稿、确认沉淀、评审、人工放行，中间任何一步没做都得被
挡下来。模型走假实现，验的是接口契约与拦阻，不是模型会不会好好说话。
"""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from atelier.agents import render, review, views, vision
from atelier.assets import characters, projects
from atelier.assets.projects import ProjectRef
from atelier.providers import image_gen, text_chat
from tests.conftest import ScriptedChat, bind_image_model, bind_text_model
from tests.test_characters import PARTIAL_BIBLE
from tests.test_render import CARD, ScriptedDraw
from tests.test_views import white_png

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

VIEW_APPROVE = """VIEW-CHECK: APPROVE

### 检查清单
- 背景纯净度：纯白
- 附属结构数量：2，符合
- 视角准确性：四个角度都对

### 修正建议
- 无
"""

VIEW_REJECT = """VIEW-CHECK: REJECT

### 检查清单
- 附属结构分离度：粘连

### 修正建议
- 背面那张往 prompt 加 two clearly separated tails
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
    bind_image_model(session, views.PAINTER, code="ark-i2i")
    bind_text_model(session, vision.REVIEWER, code="bailian-vision")


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
    response = client.post("/api/projects/demo/characters", json={"name": name})
    assert response.status_code == 201, response.text
    return dict(response.json())


def settle_spec(client: TestClient, character_id: str, chat: ScriptedChat) -> None:
    """走完设定会话那条路：聊出草稿、确认沉淀，让 `spec_path` 落到库行上。

    不直接改库：门禁确认的就是「用户按过确认沉淀」这件事，绕过它测出来的通过没有意义。
    """
    chat.replies.append(SPEC_REPLY)
    opened = client.post(
        "/api/projects/demo/conversations",
        json={"agent_code": WRITER, "target_kind": "character", "target_ref": character_id},
    )
    assert opened.status_code == 201, opened.text
    cid = opened.json()["conversation"]["id"]
    turn = client.post(
        f"/api/projects/demo/conversations/{cid}/messages", json={"content": "先出一版"}
    )
    assert turn.status_code == 200, turn.text
    committed = client.post(f"/api/projects/demo/conversations/{cid}/commit", json={})
    assert committed.status_code == 200, committed.text


# --------------------------------------------------------------------------- #
# 建角色
# --------------------------------------------------------------------------- #


def test_项目还没写视觉规范就不给建角色(
    client: TestClient, project: ProjectRef, candidates: None
) -> None:
    """art bible 是设定的风格锚点，拿模板原样当锚点等于没有锚点。"""
    response = client.post("/api/projects/demo/characters", json={"name": "赤瞳"})

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

    again = client.post("/api/projects/demo/characters", json={"name": "赤瞳"})

    assert again.status_code == 409


def test_建在分组下且跨组允许同名(client: TestClient, ready: ProjectRef, candidates: None) -> None:
    hero = client.post("/api/projects/demo/characters", json={"name": "赤瞳", "group": "玩家角色"})
    boss = client.post("/api/projects/demo/characters", json={"name": "赤瞳", "group": "boss角色"})

    assert hero.status_code == 201, hero.text
    assert boss.status_code == 201, boss.text
    assert hero.json()["dir_name"] == "characters/玩家角色/赤瞳"
    assert boss.json()["dir_name"] == "characters/boss角色/赤瞳"


def test_覆盖为真时删旧重建(client: TestClient, ready: ProjectRef, candidates: None) -> None:
    first = create(client)

    again = client.post("/api/projects/demo/characters", json={"name": "赤瞳", "overwrite": True})

    assert again.status_code == 201, again.text
    assert again.json()["id"] != first["id"]


def test_同路径孤儿不阻止新建且仍可手动删除(client: TestClient, ready: ProjectRef) -> None:
    first = create(client)
    shutil.rmtree(ready.dir / str(first["dir_name"]))

    rebuilt = client.post("/api/projects/demo/characters", json={"name": "赤瞳"})
    assert rebuilt.status_code == 201, rebuilt.text
    assert rebuilt.json()["id"] != first["id"]

    scanned = client.post("/api/projects/demo/scan")

    assert scanned.status_code == 200, scanned.text
    assert scanned.json()["missing"] == [
        {"id": first["id"], "name": "赤瞳", "dir_name": "characters/赤瞳"}
    ]

    removed = client.delete(f"/api/projects/demo/characters/{first['id']}")

    assert removed.status_code == 204, removed.text
    assert [row["id"] for row in client.get("/api/projects/demo/characters").json()] == [
        rebuilt.json()["id"]
    ]


def test_角色目录仍存在时不能只删除数据库记录(client: TestClient, ready: ProjectRef) -> None:
    character = create(client)

    response = client.delete(f"/api/projects/demo/characters/{character['id']}")

    assert response.status_code == 409
    assert "角色目录仍存在" in response.json()["detail"]
    assert len(client.get("/api/projects/demo/characters").json()) == 1


def test_分组接口列出并新建空分组(client: TestClient, ready: ProjectRef, candidates: None) -> None:
    assert client.get("/api/projects/demo/groups").json() == []

    created = client.post("/api/projects/demo/groups", json={"path": "boss角色/精英"})

    assert created.status_code == 201, created.text
    assert created.json() == ["boss角色", "boss角色/精英"]
    assert client.get("/api/projects/demo/groups").json() == ["boss角色", "boss角色/精英"]


def test_不存在的角色是404(client: TestClient, ready: ProjectRef) -> None:
    assert client.get("/api/projects/demo/characters/nope").status_code == 404


def test_列表与详情给的是同一份字段(
    client: TestClient, ready: ProjectRef, candidates: None
) -> None:
    """两处各拼一遍的话，加字段时总会只加到其中一边。"""
    created = create(client)

    listed = client.get("/api/projects/demo/characters")
    detail = client.get(f"/api/projects/demo/characters/{created['id']}")

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

    response = client.post(f"/api/projects/demo/characters/{character['id']}/review", json={})

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
    client.post(f"/api/projects/demo/characters/{character['id']}/review", json={})

    after = client.get(f"/api/projects/demo/characters/{character['id']}").json()

    assert after["state"] == characters.SPEC_DRAFTING
    assert after["gate_spec_confirmed_at"] is None
    assert [one["item"] for one in after["hard_constraints"]] == ["尾巴", "眼睛"]


def test_一个字都没有时评审说不出话(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    character = create(client)

    response = client.post(f"/api/projects/demo/characters/{character['id']}/review", json={})

    assert response.status_code == 409
    assert "还没有设定内容可审" in response.json()["detail"]


def test_裁决全文留在事件时间线里(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    """日后要回答「这份设定当时凭什么过的」，只有这条线答得上。"""
    character = create(client)
    settle_spec(client, str(character["id"]), chat)
    chat.replies.append(REJECT_REPLY)
    client.post(f"/api/projects/demo/characters/{character['id']}/review", json={})

    events = client.get(f"/api/projects/demo/characters/{character['id']}/events").json()

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
        "/api/projects/demo/conversations",
        json={"agent_code": WRITER, "target_kind": "character", "target_ref": character["id"]},
    )
    cid = opened.json()["conversation"]["id"]
    chat.replies.append(SPEC_REPLY)
    client.post(f"/api/projects/demo/conversations/{cid}/messages", json={"content": "先出一版"})
    chat.replies.extend([REJECT_REPLY, SPEC_REPLY, APPROVE_REPLY])

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/review", json={"conversation_id": cid}
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
        f"/api/projects/demo/characters/{character['id']}/review", json={"conversation_id": "nope"}
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 门禁 1
# --------------------------------------------------------------------------- #


def test_没沉淀过设定就确认不了(client: TestClient, ready: ProjectRef, candidates: None) -> None:
    """门禁确认的是磁盘上那一份；库里躺着的草稿不算。"""
    character = create(client)

    response = client.post(f"/api/projects/demo/characters/{character['id']}/spec/confirm", json={})

    assert response.status_code == 409
    assert "确认沉淀" in response.json()["detail"]


def test_人工确认后进设定已确认(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    character = create(client)
    settle_spec(client, str(character["id"]), chat)

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/spec/confirm",
        json={"note": "看过了，可以"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == characters.SPEC_CONFIRMED
    assert body["state_label"] == "设定已确认"
    assert body["gate_spec_confirmed_at"] is not None
    events = client.get(f"/api/projects/demo/characters/{character['id']}/events").json()
    gate = [one for one in events if one["event"] == "gate_spec_confirmed"]
    assert gate[-1]["message"] == "看过了，可以"


def test_确认过一次就不再确认(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    character = create(client)
    settle_spec(client, str(character["id"]), chat)
    client.post(f"/api/projects/demo/characters/{character['id']}/spec/confirm", json={})

    again = client.post(f"/api/projects/demo/characters/{character['id']}/spec/confirm", json={})

    assert again.status_code == 409


def test_驳回不动状态但留下理由(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    """驳回不是一个新阶段，是「这一步还没过」。"""
    character = create(client)
    settle_spec(client, str(character["id"]), chat)

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/spec/reject",
        json={"note": "环境设定还没写"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == characters.SPEC_DRAFTING
    events = client.get(f"/api/projects/demo/characters/{character['id']}/events").json()
    assert events[-1]["event"] == "gate_spec_rejected"
    assert events[-1]["message"] == "环境设定还没写"


def test_驳回得写清哪里不行(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    character = create(client)

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/spec/reject", json={"note": "  "}
    )

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
    client.post(f"/api/projects/demo/characters/{character['id']}/review", json={})

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/advance",
        json={"state": "S2_render_generated"},
    )

    assert response.status_code == 409
    assert "设定对焦中" in response.json()["detail"]


def test_确认之后才能推到下一步(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    character = create(client)
    settle_spec(client, str(character["id"]), chat)
    client.post(f"/api/projects/demo/characters/{character['id']}/spec/confirm", json={})

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/advance",
        json={"state": "S2_render_generated"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "S2_render_generated"


def test_不许跳级(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat
) -> None:
    character = create(client)
    settle_spec(client, str(character["id"]), chat)
    client.post(f"/api/projects/demo/characters/{character['id']}/spec/confirm", json={})

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/advance",
        json={"state": "S4_views_generated"},
    )

    assert response.status_code == 409
    assert "中间还有步骤没做" in response.json()["detail"]


def test_不认识的状态不当成还没开始(
    client: TestClient, ready: ProjectRef, candidates: None
) -> None:
    character = create(client)

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/advance", json={"state": "S99"}
    )

    assert response.status_code == 409


# --------------------------------------------------------------------------- #
# 渲染图与门禁 2
# --------------------------------------------------------------------------- #


def confirmed(client: TestClient, chat: ScriptedChat) -> dict[str, object]:
    """设定已经过了门禁 1 的角色，够开工出渲染图。"""
    character = create(client)
    settle_spec(client, str(character["id"]), chat)
    passed = client.post(f"/api/projects/demo/characters/{character['id']}/spec/confirm", json={})
    assert passed.status_code == 200, passed.text
    return character


def test_设定没确认就生不了图(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    """这是门禁 1 在 HTTP 层的另一个出口：不能绕过 advance 直接生图。"""
    character = create(client)
    settle_spec(client, str(character["id"]), chat)

    response = client.post(f"/api/projects/demo/characters/{character['id']}/render", json={})

    assert response.status_code == 409
    assert "才能生成渲染图" in response.json()["detail"]
    assert draw.calls == []


def test_卡片可以先看不生图(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    """prompt 少一截层序，用户在图上只能看出「不对」而看不出「哪里不对」。"""
    character = confirmed(client, chat)
    chat.replies.append(CARD)

    response = client.post(f"/api/projects/demo/characters/{character['id']}/asset-spec", json={})

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

    response = client.post(f"/api/projects/demo/characters/{character['id']}/render", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert "/tmp/" in body["file_path"]
    assert body["spec"]["code"] == "ASSET-DEMO-001"
    assert body["params"]["model"]
    listed = client.get(f"/api/projects/demo/characters/{character['id']}/renders").json()
    assert [one["id"] for one in listed] == [body["generation_id"]]
    assert listed[0]["is_final"] is False


def test_图本体按原格式发出去(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    """一张 2048 的 png 动辄几 MB，转 base64 塞进 JSON 再膨 33%。"""
    character = confirmed(client, chat)
    chat.replies.append(CARD)
    rendered = client.post(
        f"/api/projects/demo/characters/{character['id']}/render", json={}
    ).json()

    response = client.get(
        f"/api/projects/demo/characters/{character['id']}/renders/{rendered['generation_id']}/image"
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == draw.data


def test_别人的产物读不到(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = confirmed(client, chat)
    chat.replies.append(CARD)
    rendered = client.post(
        f"/api/projects/demo/characters/{character['id']}/render", json={}
    ).json()
    other = create(client, "青瞳")

    response = client.get(
        f"/api/projects/demo/characters/{other['id']}/renders/{rendered['generation_id']}/image"
    )

    assert response.status_code == 404


def test_采用得指名哪一张(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    """默认采用「最新一张」在用户连生了几张之后就不是他指的那一张了。"""
    character = confirmed(client, chat)
    chat.replies.append(CARD)
    client.post(f"/api/projects/demo/characters/{character['id']}/render", json={})

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/render/confirm", json={}
    )

    assert response.status_code == 422


def test_采用之后进渲染图已确认(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = confirmed(client, chat)
    chat.replies.append(CARD)
    rendered = client.post(
        f"/api/projects/demo/characters/{character['id']}/render", json={}
    ).json()

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/render/confirm",
        json={"generation_id": rendered["generation_id"], "note": "就这张"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == characters.RENDER_CONFIRMED
    assert body["state_label"] == "渲染图已定稿"
    assert body["render_path"] == "characters/赤瞳/images/character_赤瞳_渲染图.png"
    assert body["gate_render_confirmed_at"] is not None
    listed = client.get(f"/api/projects/demo/characters/{character['id']}/renders").json()
    assert listed[0]["is_final"] is True
    assert listed[0]["file_path"] == body["render_path"]


def test_不存在的产物采用不了(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = confirmed(client, chat)
    chat.replies.append(CARD)
    client.post(f"/api/projects/demo/characters/{character['id']}/render", json={})

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/render/confirm",
        json={"generation_id": "nope"},
    )

    assert response.status_code == 404


def test_驳回渲染图时状态停在S2(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = confirmed(client, chat)
    chat.replies.append(CARD)
    client.post(f"/api/projects/demo/characters/{character['id']}/render", json={})

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/render/reject",
        json={"note": "尾巴粘在一起了"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == characters.RENDER_GENERATED
    assert response.json()["render_path"] is None
    events = client.get(f"/api/projects/demo/characters/{character['id']}/events").json()
    assert events[-1]["event"] == "gate_render_rejected"
    assert events[-1]["message"] == "尾巴粘在一起了"


# --------------------------------------------------------------------------- #
# 四视图
# --------------------------------------------------------------------------- #


def staged(client: TestClient, chat: ScriptedChat, draw: ScriptedDraw) -> dict[str, object]:
    """渲染图已经定稿（S3）的角色，够开工出四视图。

    假图改成卡片写的那个画幅与纯白底：四视图要拿它当参考图，尺寸对不上或底不白会在结果里
    多出一堆说明，把接口契约那几条断言泷得看不清。
    """
    character = confirmed(client, chat)
    chat.replies.append(CARD)
    draw.data = white_png(2048, 2048)
    body = client.post(f"/api/projects/demo/characters/{character['id']}/render", json={}).json()
    passed = client.post(
        f"/api/projects/demo/characters/{character['id']}/render/confirm",
        json={"generation_id": body["generation_id"]},
    )
    assert passed.status_code == 200, passed.text
    return character


def four(client: TestClient, character_id: str) -> dict[str, object]:
    response = client.post(f"/api/projects/demo/characters/{character_id}/views", json={})
    assert response.status_code == 200, response.text
    return dict(response.json())


def picks(client: TestClient, character_id: str) -> dict[str, str]:
    """每个视角最新那一张，当作用户在界面上挑的那一组。"""
    listed = client.get(f"/api/projects/demo/characters/{character_id}/views").json()
    chosen: dict[str, str] = {}
    for one in listed:
        chosen.setdefault(str(one["variant"]), str(one["id"]))
    return chosen


def test_渲染图没定稿就出不了四视图(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    """四视图拿定稿渲染图当参考，没它就退化成纯文字生成，四张图会各说各话。"""
    character = confirmed(client, chat)

    response = client.post(f"/api/projects/demo/characters/{character['id']}/views", json={})

    assert response.status_code == 409
    assert "才能生成四视图" in response.json()["detail"]
    assert draw.calls == []


def test_一次出四张并推到S4(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = staged(client, chat, draw)

    body = four(client, str(character["id"]))

    assert [one["variant"] for one in body["images"]] == [one.code for one in views.VARIANTS]
    assert [one["label"] for one in body["images"]] == [one.label for one in views.VARIANTS]
    assert body["failures"] == []
    assert len(body["references"]) == 2, "姿势模版与定稿渲染图两张都必传"
    assert body["state"] == characters.VIEWS_GENERATED
    assert body["state_label"] == "四视图已生成"
    assert body["ok"] is True
    assert body["size_complaint"] is None
    assert all("/tmp/" in one["file_path"] for one in body["images"])
    assert all(one["problems"] == [] for one in body["images"])


def test_不认识的视角当场报错(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    """静静跳过的后果是用户点了「重生背面」但什么都没发生，而界面上看不出来。"""
    character = staged(client, chat, draw)
    before = len(draw.calls)

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/views", json={"variants": ["back-left"]}
    )

    assert response.status_code == 409
    assert "不认识的视角" in response.json()["detail"]
    assert len(draw.calls) == before


def test_只重生点名的那一个视角(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = staged(client, chat, draw)
    four(client, str(character["id"]))
    before = len(draw.calls)

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/views", json={"variants": ["back"]}
    )

    assert response.status_code == 200, response.text
    assert [one["variant"] for one in response.json()["images"]] == ["back"]
    assert len(draw.calls) == before + 1


def test_候选列表标出是哪个面(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    """不标视角的话前端只能拿文件名猜，而文件名是落盘细节。"""
    character = staged(client, chat, draw)
    four(client, str(character["id"]))

    listed = client.get(f"/api/projects/demo/characters/{character['id']}/views").json()

    assert sorted(one["variant"] for one in listed) == sorted(one.code for one in views.VARIANTS)
    assert all(one["is_final"] is False for one in listed)


def test_评审给出裁决与理由(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = staged(client, chat, draw)
    four(client, str(character["id"]))
    chat.replies.append(VIEW_APPROVE)

    response = client.post(f"/api/projects/demo/characters/{character['id']}/views/review", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] == "APPROVE"
    assert body["approved"] is True
    assert body["mode"] == vision.LEAN
    assert body["skipped"] is False
    assert body["verdicts"][0]["variants"] == [one.code for one in views.VARIANTS]
    assert body["verdicts"][0]["text"] == VIEW_APPROVE.strip()
    assert "检查清单" in body["verdicts"][0]["sections"]


def test_驳回也不定稿也不自己重生(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    """重生要花额度，该不该花得用户说了算；裁决只能拦不能放行。"""
    character = staged(client, chat, draw)
    four(client, str(character["id"]))
    before = len(draw.calls)
    chat.replies.append(VIEW_REJECT)

    body = client.post(
        f"/api/projects/demo/characters/{character['id']}/views/review", json={}
    ).json()

    assert body["decision"] == "REJECT"
    assert body["regenerated"] == 0
    assert len(draw.calls) == before
    after = client.get(f"/api/projects/demo/characters/{character['id']}").json()
    assert after["state"] == characters.VIEWS_GENERATED
    assert after["view_paths"] == {}


def test_驳回后可以让平台自己重生(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = staged(client, chat, draw)
    four(client, str(character["id"]))
    before = len(draw.calls)
    chat.replies.extend([VIEW_REJECT, VIEW_APPROVE])

    body = client.post(
        f"/api/projects/demo/characters/{character['id']}/views/review", json={"regenerate": True}
    ).json()

    assert body["decision"] == "APPROVE"
    assert body["regenerated"] == 1
    assert len(draw.calls) == before + 1, "只重生被点名的背面那一张"


def test_定稿四张进定稿位并推到S5(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = staged(client, chat, draw)
    four(client, str(character["id"]))

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/views/confirm",
        json={"picks": picks(client, str(character["id"])), "note": "就这一组"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == characters.VIEWS_CONFIRMED
    assert body["state_label"] == "四视图已确认"
    assert sorted(body["view_paths"]) == sorted(one.code for one in views.VARIANTS)
    assert all(one.startswith("characters/赤瞳/images/") for one in body["view_paths"].values())
    listed = client.get(f"/api/projects/demo/characters/{character['id']}/views").json()
    assert sum(1 for one in listed if one["is_final"]) == 4


def test_四个角度不齐就不给定稿(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    """只定两张等于允许一组里两张新两张旧，而新旧混用出来的模型是错的却看不出为什么。"""
    character = staged(client, chat, draw)
    four(client, str(character["id"]))
    chosen = picks(client, str(character["id"]))
    chosen.pop("back")

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/views/confirm", json={"picks": chosen}
    )

    assert response.status_code == 409
    assert "还差 背面" in response.json()["detail"]


def test_不存在的产物定不了稿(
    client: TestClient, ready: ProjectRef, candidates: None, chat: ScriptedChat, draw: ScriptedDraw
) -> None:
    character = staged(client, chat, draw)
    four(client, str(character["id"]))
    chosen = picks(client, str(character["id"]))
    chosen["front"] = "nope"

    response = client.post(
        f"/api/projects/demo/characters/{character['id']}/views/confirm", json={"picks": chosen}
    )

    assert response.status_code == 404
