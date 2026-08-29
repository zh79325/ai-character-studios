"""额度账务：本地镜像判定、远程口径覆写、标满后不再问远程。"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from atelier.providers import usage
from atelier.providers.usage_client import Permit, key_id_of, mask_key, service_of
from tests.conftest import StubUsageClient, make_model, make_provider


def test_no_limit_means_no_accounting(session: Session) -> None:
    """未配 model_limits 就是不限量：不统计、不拦截。"""
    provider = make_provider(session, "ark")
    model = make_model(session, provider, "glm-5.3")

    assert usage.has_budget(session, model, "tokens")
    seen = usage.peek(session, model, "tokens")
    assert seen.unlimited and seen.available is None
    assert usage.reserve(session, model, "calls").granted
    assert usage.record(session, model, "tokens", 999).unlimited


def test_local_accounting_accumulates_and_stops_at_limit(session: Session) -> None:
    provider = make_provider(session, "ark")
    model = make_model(session, provider, "glm-5.3", limit=("tokens", 100, "day"))

    assert usage.record(session, model, "tokens", 60).used == 60
    assert usage.has_budget(session, model, "tokens")
    assert usage.record(session, model, "tokens", 30).used == 90

    # 第三次装不下：拒发即标满，本窗口停用
    result = usage.record(session, model, "tokens", 30)
    assert not result.granted
    assert result.used == 100
    assert result.exhausted_at is not None
    assert not usage.has_budget(session, model, "tokens")


def test_reserve_pre_deducts_before_the_call(session: Session) -> None:
    """按次计费的口径在调用前就扣，扣不动就是没额度。"""
    provider = make_provider(session, "ark_agent")
    model = make_model(session, provider, "doubao-seedream-5.0-lite", limit=("calls", 2, "day"))

    assert usage.reserve(session, model, "calls").granted
    assert usage.reserve(session, model, "calls").granted
    assert not usage.reserve(session, model, "calls").granted


def test_credits_pre_deduct_by_operation_price(session: Session) -> None:
    """Meshy：每种请求消耗多少积分配在 params.credit_costs，总积分配在 model_limits。"""
    provider = make_provider(session, "meshy", driver="meshy")
    model = make_model(
        session,
        provider,
        "meshy-5",
        params={"credit_costs": {"image_to_3d": 5, "animate": 10}},
        limit=("credits", 12, "total"),
    )

    assert model.credit_cost("image_to_3d") == 5
    assert model.credit_cost("animate") == 10
    assert model.credit_cost("没配过的操作") == 0

    assert usage.reserve(session, model, "credits", delta=5).granted
    assert not usage.reserve(session, model, "credits", delta=10).granted


def test_total_period_never_rolls_over(session: Session) -> None:
    """买断式积分池不按窗口重置，用掉就是用掉了。"""
    provider = make_provider(session, "meshy", driver="meshy")
    model = make_model(session, provider, "meshy-5", limit=("credits", 10, "total"))

    usage.reserve(session, model, "credits", delta=10)
    seen = usage.peek(session, model, "credits")
    assert seen.window_key == "total"
    assert seen.available == 0


def test_mark_exhausted_beats_local_count(session: Session) -> None:
    """接口实报额度用尽时官方口径优先，直接标满让其他机器也跳过。"""
    provider = make_provider(session, "ark")
    model = make_model(session, provider, "glm-5.3", limit=("tokens", 1000, "day"))

    usage.record(session, model, "tokens", 10)
    result = usage.mark_exhausted(session, model, "tokens")
    assert result.used == 1000
    assert result.available == 0
    assert not usage.has_budget(session, model, "tokens")


def test_remaining_header_overrides_local_count(session: Session) -> None:
    """供应商报的剩余额度比本地累加准，落进镜像后按它判定。"""
    provider = make_provider(session, "ark")
    model = make_model(session, provider, "glm-5.3", limit=("tokens", 1000, "day"))

    usage.record(session, model, "tokens", 10)
    result = usage.apply_remaining(session, model, "tokens", remaining=200)
    assert result.source == "header"
    assert result.available == 200
    assert usage.has_budget(session, model, "tokens", need=200)
    assert not usage.has_budget(session, model, "tokens", need=201)


def test_zero_remaining_marks_exhausted(session: Session) -> None:
    provider = make_provider(session, "ark")
    model = make_model(session, provider, "glm-5.3", limit=("tokens", 1000, "day"))

    result = usage.apply_remaining(session, model, "tokens", remaining=0)
    assert not result.granted
    assert not usage.has_budget(session, model, "tokens")


def test_remote_permit_overwrites_local_mirror(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """远程是真相：它说用了 777，本地就记 777，不管本地原来算到多少。"""
    provider = make_provider(session, "ark")
    model = make_model(session, provider, "glm-5.3", limit=("tokens", 1000, "day"))

    stub = StubUsageClient([Permit(granted=True, used=777, limit=1000, remaining=223)])
    monkeypatch.setattr(usage, "get_usage_client", lambda: stub)

    result = usage.record(session, model, "tokens", 5)
    assert result.used == 777
    assert result.remaining == 223
    assert result.source == "remote"
    assert stub.calls[0]["service"] == "llm_model_tokens:ark:glm-5.3"
    assert stub.calls[0]["delta"] == 5


def test_limit_always_comes_from_local_config(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """远程存的 limit 只是旧快照，上限调大后照抄它会把新额度按回旧上限。"""
    provider = make_provider(session, "ark")
    model = make_model(session, provider, "glm-5.3", limit=("tokens", 1_800_000, "day"))

    stub = StubUsageClient([Permit(granted=True, used=600_000, limit=500_000)])
    monkeypatch.setattr(usage, "get_usage_client", lambda: stub)

    result = usage.record(session, model, "tokens", 1)
    assert result.limit == 1_800_000
    assert result.available == 1_200_000
    assert usage.has_budget(session, model, "tokens")


def test_exhausted_mirror_short_circuits_remote(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地已标满就不必再问远程，一个请求都不打。"""
    provider = make_provider(session, "ark")
    model = make_model(session, provider, "glm-5.3", limit=("tokens", 10, "day"))
    usage.mark_exhausted(session, model, "tokens")

    stub = StubUsageClient([Permit(granted=True, used=0, limit=10)])
    monkeypatch.setattr(usage, "get_usage_client", lambda: stub)

    assert not usage.reserve(session, model, "tokens").granted
    assert stub.calls == []


def test_snapshot_syncs_used_without_deducting(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """peek 只读不扣：远程报多少用量就同步多少。"""
    provider = make_provider(session, "ark", api_key="sk-abcdefgh1234")
    model = make_model(session, provider, "glm-5.3", limit=("tokens", 1000, "day"))

    from atelier.providers import period as period_mod

    stub = StubUsageClient()
    stub.snapshot_items = [
        {
            "keyId": key_id_of("sk-abcdefgh1234"),
            "keyMask": mask_key("sk-abcdefgh1234"),
            "limitKey": period_mod.window_label("day"),
            "limit": 1000,
            "quta": 432,
        }
    ]
    monkeypatch.setattr(usage, "get_usage_client", lambda: stub)

    seen = usage.peek(session, model, "tokens")
    assert seen.used == 432
    assert seen.source == "remote"
    assert stub.calls == []


def test_stale_window_in_snapshot_counts_as_zero(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """远程记的是上一个窗口，当前窗口就是 0，不能照抄旧值。"""
    provider = make_provider(session, "ark", api_key="sk-abcdefgh1234")
    model = make_model(session, provider, "glm-5.3", limit=("tokens", 1000, "day"))

    stub = StubUsageClient()
    stub.snapshot_items = [
        {
            "keyId": key_id_of("sk-abcdefgh1234"),
            "limitKey": "1999-01-01",
            "limit": 1000,
            "quta": 999,
        }
    ]
    monkeypatch.setattr(usage, "get_usage_client", lambda: stub)

    assert usage.peek(session, model, "tokens").used == 0


def test_real_key_never_leaves_the_machine() -> None:
    """只发 md5 与掩码，真实 key 不出本机。"""
    key = "sk-sp-1234567890abcdef"
    assert key not in key_id_of(key)
    assert len(key_id_of(key)) == 32
    assert mask_key(key) == "sk-s***cdef"
    assert mask_key("") == "***"


def test_service_is_per_provider_and_model() -> None:
    """service 命名与其他工具一致，同一批 key 的额度才是合并统计的。"""
    assert service_of("ark_coding", "glm-5.3") == "llm_model_tokens:ark_coding:glm-5.3"
