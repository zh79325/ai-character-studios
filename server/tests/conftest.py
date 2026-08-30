"""路由层与 API 测试的共用夹具：临时三库 + 断网的用量服务。

用量服务默认不可用，账务全走本地镜像——这是远程挂掉时的兜底路径，也是单测里唯一
不依赖外部服务的路径。要测远程口径的用例自己装一个假客户端。

API 测试用 `client` 夹具：把全局两库的依赖换成临时库的 Session，不碰 db/ 下的真库。项目库
不需要覆盖：它本来就住在项目目录里，把项目建在 tmp_path 下就已经是隔离的（见 `project`
夹具）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from atelier.agents.stream_bus import BUS
from atelier.assets import projects as projects_mod
from atelier.db.config_models import ConfigBase
from atelier.db.project_models import ProjectBase
from atelier.db.runtime_models import (
    ModelLimit,
    Provider,
    ProviderAgentModel,
    ProviderModel,
    RuntimeBase,
)
from atelier.db.session import dispose_project_engines, project_engine
from atelier.providers import usage
from atelier.providers.base import Candidate
from atelier.providers.text_chat import ChatReply
from atelier.providers.usage_client import Permit
from atelier.settings import get_settings


class OfflineUsageClient:
    """远程用量服务不可用：一律返回 None，交给本地镜像判定。"""

    def acquire(self, *args: Any, **kwargs: Any) -> Permit | None:
        return None

    def snapshot(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]] | None:
        return None


class StubUsageClient:
    """按脚本回答的假用量服务，用来验证「远程口径覆写本地镜像」。"""

    def __init__(self, permits: list[Permit] | None = None) -> None:
        self.permits = permits or []
        self.snapshot_items: list[dict[str, Any]] | None = None
        self.calls: list[dict[str, Any]] = []

    def acquire(
        self,
        service: str,
        api_key: str,
        period: str,
        max_value: int,
        delta: int = 1,
        exhausted: bool = False,
    ) -> Permit | None:
        self.calls.append(
            {
                "service": service,
                "period": period,
                "max_value": max_value,
                "delta": delta,
                "exhausted": exhausted,
            }
        )
        return self.permits.pop(0) if self.permits else None

    def snapshot(self, service: str, period: str) -> list[dict[str, Any]] | None:
        return self.snapshot_items


def _file_engine(path: Path) -> Engine:
    """落在临时目录的 sqlite 库。

    不用 `sqlite://` 内存库：TestClient 在另一个线程里跑应用，内存库要么跨线程看不见
    （默认池）、要么得把同一个连接两边抢（StaticPool），而 SSE 长连接测试就是要一边读
    一边写。文件库 + WAL 才能让读写各占自己的连接。
    """
    eng = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        dbapi_conn.execute("PRAGMA busy_timeout=5000")

    return eng


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = _file_engine(tmp_path / "runtime.db")
    RuntimeBase.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s


@pytest.fixture
def cfg_engine(tmp_path: Path) -> Engine:
    eng = _file_engine(tmp_path / "config.db")
    ConfigBase.metadata.create_all(eng)
    return eng


@pytest.fixture
def cfg_session(cfg_engine: Engine) -> Iterator[Session]:
    with Session(cfg_engine) as s:
        yield s


@pytest.fixture(autouse=True)
def release_project_engines() -> Iterator[None]:
    """用完就放开项目库句柄。

    engine 是按库路径缓存的进程级字典，而每个用例的项目都在新的 tmp_path 里，不收就是
    一路累连接池，整轮跑下来容易碰文件句柄上限。
    """
    yield
    dispose_project_engines()


@pytest.fixture
def projects_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把默认项目根指到临时目录，并让建项目库走 create_all 而不跑 alembic。

    不指开就会在仓库真的 `assets/` 下建测试项目；不换掉 alembic 则每建一个项目就要跑一轮
    迁移，十几个用例加起来很浪费。「迁移真的能建出同一套表」由 test_migrations 单独盯。
    """
    root = tmp_path / "projects"
    root.mkdir()
    settings = get_settings().model_copy(update={"projects_root": root})

    def fake_upgrade(db_path: Path, revision: str = "head") -> None:
        ProjectBase.metadata.create_all(project_engine(db_path))

    monkeypatch.setattr(projects_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(projects_mod, "upgrade_project", fake_upgrade)
    monkeypatch.setattr("atelier.api.projects.get_settings", lambda: settings)
    return root


@pytest.fixture
def project(session: Session, projects_root: Path) -> projects_mod.ProjectRef:
    """一个建在临时项目根下、已登记且已设为当前的项目。"""
    ref = projects_mod.create_project(session, name="测试项目", code="demo")
    projects_mod.open_project(session, ref.code)
    session.commit()
    return ref


@pytest.fixture
def project_db(project: projects_mod.ProjectRef) -> Iterator[Session]:
    """项目库的 Session。项目库住在项目目录里，跟着 `project` 夹具一起隔离。"""
    with Session(project_engine(project.db_path)) as s:
        yield s


@pytest.fixture(autouse=True)
def quiet_bus() -> Iterator[None]:
    """广播缓冲是进程级单例，用例之间必须清干净。

    不清就会串味：上一个用例发布的 delta 还留在缓冲里，下一个用例按 `after_seq=0` 订流
    就先读到别人的字。
    """
    BUS.clear()
    yield
    BUS.clear()


class ScriptedChat:
    """按脚本回答的假模型，签名与 `text_chat.complete` 一致。

    记下每次收到的 `messages`：会话的核心约定是「上下文按固定顺序拼」，只断言最终落库的
    内容盖不住它——模型到底看见了什么才是要钉住的东西。
    """

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []
        self.deltas: list[str] = []
        self.default = "好的。"

    def __call__(
        self,
        candidate: Candidate,
        messages: Any,
        *,
        on_delta: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> ChatReply:
        self.calls.append([dict(m) for m in messages])
        content = self.replies.pop(0) if self.replies else self.default
        if on_delta is not None:
            for piece in content.splitlines(keepends=True):
                self.deltas.append(piece)
                on_delta(piece)
        return ChatReply(
            content=content,
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            latency_ms=5,
        )

    @property
    def system_of_last(self) -> str:
        return self.calls[-1][0]["content"]


def bind_text_model(
    session: Session,
    agent_code: str,
    *,
    code: str = "bailian",
    model_id: str = "qwen-plus",
    priority: int = 100,
    limit: tuple[str, int, str] | None = None,
) -> ProviderModel:
    """给某个文本 Agent 备一个可用候选。"""
    provider = make_provider(session, code, priority=priority)
    return make_model(session, provider, model_id, agent_code=agent_code, limit=limit)


@pytest.fixture
def client(engine: Engine, cfg_engine: Engine) -> Iterator[TestClient]:
    """指向临时全局两库的 HTTP 客户端，不碰 db/ 下的真库。

    每个请求开自己的 Session（与真实依赖一致），不把测试线程的 Session 借给应用线程——
        Session 不是线程安全的，SSE 测试里两边会同时用库。

    全局库那一份要跟真依赖一样在请求结尾提交：不提交的话路由层自己 commit 的东西看得见、
    API 层改的东西却静静丢掉，下一个请求就像什么都没发生过。
    """
    from atelier.api.deps import config_db, runtime_db
    from atelier.main import app

    def _open(target: Engine, *, commit: bool) -> Callable[[], Iterator[Session]]:
        def dependency() -> Iterator[Session]:
            with Session(target) as s:
                try:
                    yield s
                    if commit:
                        s.commit()
                except Exception:
                    s.rollback()
                    raise

        return dependency

    app.dependency_overrides[runtime_db] = _open(engine, commit=True)
    app.dependency_overrides[config_db] = _open(cfg_engine, commit=False)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def offline_usage(monkeypatch: pytest.MonkeyPatch) -> OfflineUsageClient:
    """默认断开远程用量服务，单测不打任何网络。"""
    client = OfflineUsageClient()
    monkeypatch.setattr(usage, "get_usage_client", lambda: client)
    return client


@pytest.fixture(autouse=True)
def no_project_opened() -> Iterator[None]:
    """「打开的是哪个项目」是进程内状态，用例之间必须清干净，否则前一个用例打开的项目会
    漏进下一个（它的临时目录早已删掉，症状是莫名的 404）。"""
    projects_mod.close_project()
    yield
    projects_mod.close_project()


def make_provider(
    session: Session,
    code: str,
    *,
    priority: int = 100,
    api_key: str = "sk-test",
    driver: str = "openai_compat",
    enabled: bool = True,
) -> Provider:
    provider = Provider(
        code=code,
        name=f"{code} 账号",
        base_url="https://example.invalid",
        api_key=api_key,
        priority=priority,
        driver=driver,
        enabled=enabled,
    )
    session.add(provider)
    session.commit()
    return provider


def make_model(
    session: Session,
    provider: Provider,
    model_id: str,
    *,
    agent_code: str | None = None,
    sort_no: int = 0,
    enabled: bool = True,
    params: dict[str, Any] | None = None,
    capabilities: list[str] | None = None,
    limit: tuple[str, int, str] | None = None,
) -> ProviderModel:
    """挂一个模型到 provider 下，可顺带绑 Agent 与配额度。

    limit 形如 ("tokens", 1000, "day")。
    """
    provider_model = ProviderModel(
        provider_code=provider.code,
        model_id=model_id,
        capabilities=capabilities or ["text"],
        sort_no=sort_no,
        enabled=enabled,
        params=params or {},
    )
    session.add(provider_model)
    session.commit()

    if agent_code is not None:
        session.add(ProviderAgentModel(agent_code=agent_code, provider_model_id=provider_model.id))
    if limit is not None:
        kind, max_value, period_expr = limit
        session.add(
            ModelLimit(
                provider_model_id=provider_model.id,
                limit_kind=kind,
                max_value=max_value,
                period_expr=period_expr,
            )
        )
    session.commit()
    return provider_model


def bind_image_model(
    session: Session,
    agent_code: str,
    *,
    code: str = "ark",
    model_id: str = "doubao-seedream-5.0-lite",
    driver: str = "ark_image",
    priority: int = 100,
    limit: tuple[str, int, str] | None = None,
) -> ProviderModel:
    """给某个生图 Agent 备一个可用候选。额度口径是 `calls`，选中即预扣。"""
    provider = make_provider(session, code, priority=priority, driver=driver)
    return make_model(
        session,
        provider,
        model_id,
        agent_code=agent_code,
        capabilities=["t2i"],
        limit=limit,
    )
