"""路由层与 API 测试的共用夹具：内存双库 + 断网的用量服务。

用量服务默认不可用，账务全走本地镜像——这是远程挂掉时的兜底路径，也是单测里唯一
不依赖外部服务的路径。要测远程口径的用例自己装一个假客户端。

API 测试用 `client` 夹具：把两个库的依赖换成内存 Session，不碰 db/ 下的真库。
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

from atelier.db.config_models import ConfigBase
from atelier.db.runtime_models import (
    ModelLimit,
    Provider,
    ProviderAgentModel,
    ProviderModel,
    RuntimeBase,
)
from atelier.providers import usage
from atelier.providers.usage_client import Permit


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


@pytest.fixture
def client(engine: Engine, cfg_engine: Engine) -> Iterator[TestClient]:
    """指向临时双库的 HTTP 客户端，不碰 db/ 下的真库。

    每个请求开自己的 Session（与真实依赖一致），不把测试线程的 Session 借给应用线程——
        Session 不是线程安全的，SSE 测试里两边会同时用库。
    """
    from atelier.api.deps import config_db, runtime_db
    from atelier.main import app

    def _open(target: Engine) -> Callable[[], Iterator[Session]]:
        def dependency() -> Iterator[Session]:
            with Session(target) as s:
                yield s

        return dependency

    app.dependency_overrides[runtime_db] = _open(engine)
    app.dependency_overrides[config_db] = _open(cfg_engine)
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
    limit: tuple[str, int, str] | None = None,
) -> ProviderModel:
    """挂一个模型到 provider 下，可顺带绑 Agent 与配额度。

    limit 形如 ("tokens", 1000, "day")。
    """
    provider_model = ProviderModel(
        provider_code=provider.code,
        model_id=model_id,
        capabilities=["text"],
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
