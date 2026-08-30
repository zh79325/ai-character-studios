"""SSE 面板：任务事件流与选路日志流。

流是靠轮询数据库增量推的，所以测试里把轮询间隔压到 0.01 秒；任务进终态后流会自己收，
不用强行掐断连接。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from atelier.api import events
from atelier.db.runtime_models import RouteLog, Task, TaskEvent


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events, "POLL_SECONDS", 0.01)


def make_task(session: Session, task_id: str, status: str = "succeeded") -> Task:
    task = Task(
        id=task_id,
        project_code="chitong",
        target_kind="character",
        target_ref="chitong_beast",
        stage="spec",
        agent_code="spec_writer",
        status=status,
    )
    session.add(task)
    session.commit()
    return task


def add_event(session: Session, task_id: str, seq: int, event: str, message: str = "") -> None:
    session.add(TaskEvent(task_id=task_id, seq=seq, event=event, message=message))
    session.commit()


def add_route_log(
    session: Session,
    *,
    agent_code: str = "spec_writer",
    outcome: str = "bound",
    task_id: str | None = None,
    ts: datetime | None = None,
) -> RouteLog:
    row = RouteLog(
        agent_code=agent_code,
        provider_code="ark",
        model_id="glm-5.3",
        outcome=outcome,
        task_id=task_id,
        ts=ts or datetime.now(),
    )
    session.add(row)
    session.commit()
    return row


# --------------------------------------------------------------------------- #
# 纯函数：游标语义
# --------------------------------------------------------------------------- #


def test_fetch_route_logs_walks_forward(session: Session) -> None:
    first = add_route_log(session)
    second = add_route_log(session, outcome="sticky_hit")

    assert [row.id for row in events.fetch_route_logs(session, 0)] == [first.id, second.id]
    assert [row.id for row in events.fetch_route_logs(session, first.id)] == [second.id]
    assert events.fetch_route_logs(session, second.id) == []


def test_fetch_route_logs_can_narrow_to_one_task(session: Session) -> None:
    add_route_log(session)
    mine = add_route_log(session, task_id="t-1")

    rows = events.fetch_route_logs(session, 0, task_id="t-1")
    assert [row.id for row in rows] == [mine.id]


def test_start_cursor_backfills_a_window(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """面板一打开就该有内容，所以不给游标时要往回补一段历史。"""
    monkeypatch.setattr(events, "BACKLOG", 2)
    ids = [add_route_log(session).id for _ in range(5)]

    assert events._start_cursor(session, None) == ids[-1] - 2
    assert events._start_cursor(session, 0) == 0  # 显式要从头看就从头看
    assert events._start_cursor(session, ids[2]) == ids[2]


def test_start_cursor_on_an_empty_table(session: Session) -> None:
    assert events._start_cursor(session, None) == 0


def test_fetch_task_events_respects_the_sequence(session: Session) -> None:
    make_task(session, "t-1")
    add_event(session, "t-1", 1, "started")
    add_event(session, "t-1", 2, "prompt_built")
    add_event(session, "other", 1, "started")

    assert [row.seq for row in events.fetch_task_events(session, "t-1", 0)] == [1, 2]
    assert [row.seq for row in events.fetch_task_events(session, "t-1", 1)] == [2]


# --------------------------------------------------------------------------- #
# 任务事件流
# --------------------------------------------------------------------------- #


def test_unknown_task_is_404(client: TestClient) -> None:
    assert client.get("/api/events/nope").status_code == 404


def test_finished_task_replays_then_closes(client: TestClient, session: Session) -> None:
    make_task(session, "t-1", status="succeeded")
    add_event(session, "t-1", 1, "started", "开工")
    add_event(session, "t-1", 2, "finished", "收工")
    add_route_log(session, task_id="t-1")
    add_route_log(session, task_id="other")  # 别的任务的日志不该混进来

    body = client.get("/api/events/t-1").text

    assert "event: ready" in body
    assert body.count("event: task_event") == 2
    assert "开工" in body and "收工" in body
    assert body.count("event: route_log") == 1
    assert body.rstrip().endswith("data: succeeded")
    assert "event: done" in body


def test_after_seq_skips_what_the_panel_already_has(client: TestClient, session: Session) -> None:
    make_task(session, "t-1")
    add_event(session, "t-1", 1, "started", "第一条")
    add_event(session, "t-1", 2, "finished", "第二条")

    body = client.get("/api/events/t-1", params={"after_seq": 1}).text
    assert "第一条" not in body
    assert "第二条" in body


def test_failed_task_also_closes_the_stream(client: TestClient, session: Session) -> None:
    make_task(session, "t-1", status="failed")
    add_event(session, "t-1", 1, "failed", "provider 502")

    body = client.get("/api/events/t-1").text
    assert "event: done" in body
    assert body.rstrip().endswith("data: failed")


def test_stream_sees_writes_that_land_after_it_started(
    client: TestClient, session: Session, engine: Engine
) -> None:
    """长连接不能被 SQLite 的只读快照钉在打开那一刻的数据上。

    开流之前挂一个定时写入：流必须看到它，并在任务转终态后收掉。
    """
    make_task(session, "t-1", status="running")
    add_event(session, "t-1", 1, "started", "开工")

    def write_later() -> None:
        with Session(engine) as late:
            late.add(TaskEvent(task_id="t-1", seq=2, event="progress", message="画到一半"))
            late.commit()
            task = late.get(Task, "t-1")
            assert task is not None
            task.status = "succeeded"
            late.commit()

    timer = threading.Timer(0.1, write_later)
    timer.start()
    try:
        body = client.get("/api/events/t-1").text
    finally:
        timer.cancel()

    assert "开工" in body
    assert "画到一半" in body
    assert "event: done" in body


# --------------------------------------------------------------------------- #
# 选路日志流
#
# 这条流永不自终，拉 HTTP 就得靠断开连接才能结束；直接驱动它的产生器更确定，
# 断开的时机也能自己说了算。
# --------------------------------------------------------------------------- #


class FakeRequest:
    """只会答「断没断开」的假请求：前 rounds 轮说没断，之后说断了。"""

    def __init__(self, rounds: int) -> None:
        self.left = rounds

    async def is_disconnected(self) -> bool:
        if self.left <= 0:
            return True
        self.left -= 1
        return False


async def drain(response: EventSourceResponse) -> list[ServerSentEvent]:
    return [chunk async for chunk in response.body_iterator]


async def test_route_log_stream_pushes_new_rows(session: Session) -> None:
    old = add_route_log(session, ts=datetime.now() - timedelta(minutes=5))
    fresh = add_route_log(session, outcome="rebound")

    response = await events.route_log_stream(
        FakeRequest(rounds=1),  # type: ignore[arg-type]
        session,
        after_id=old.id,
        agent_code=None,
    )
    chunks = await drain(response)

    assert [chunk.event for chunk in chunks] == ["ready", "route_log"]
    assert chunks[0].data == str(old.id)
    assert f'"id":{fresh.id}' in str(chunks[1].data)
    assert "rebound" in str(chunks[1].data)


async def test_route_log_stream_can_filter_by_agent(session: Session) -> None:
    add_route_log(session, agent_code="spec_writer")
    wanted = add_route_log(session, agent_code="prompt_smith")

    response = await events.route_log_stream(
        FakeRequest(rounds=1),  # type: ignore[arg-type]
        session,
        after_id=0,
        agent_code="prompt_smith",
    )
    chunks = await drain(response)

    payloads = [str(chunk.data) for chunk in chunks if chunk.event == "route_log"]
    assert len(payloads) == 1
    assert f'"id":{wanted.id}' in payloads[0]
    assert "spec_writer" not in payloads[0]


async def test_route_log_stream_backfills_when_no_cursor_given(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不给游标时面板一打开就要有内容。"""
    monkeypatch.setattr(events, "BACKLOG", 2)
    for _ in range(5):
        add_route_log(session)

    response = await events.route_log_stream(
        FakeRequest(rounds=1),  # type: ignore[arg-type]
        session,
        after_id=None,
        agent_code=None,
    )
    chunks = await drain(response)

    assert len([c for c in chunks if c.event == "route_log"]) == 2
