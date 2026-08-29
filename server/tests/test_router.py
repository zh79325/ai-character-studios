"""选路：会话粘性、严格 priority、换绑优先同模型、熔断与额度闸门、route_logs。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from atelier.db.runtime_models import CircuitBreaker, Conversation, RouteLog
from atelier.providers import router, usage
from atelier.providers.base import (
    CallOutcome,
    NoCandidateError,
    ProviderError,
    QuotaExhausted,
    RetryableError,
)
from tests.conftest import make_model, make_provider

AGENT = "spec_writer"


def _conversation(session: Session, cid: str = "c1") -> Conversation:
    row = Conversation(
        id=cid,
        project_code="chitong",
        target_kind="character",
        target_ref="chitong_shuangweishou",
        agent_code=AGENT,
    )
    session.add(row)
    session.commit()
    return row


def _logs(session: Session) -> list[RouteLog]:
    return list(session.scalars(select(RouteLog).order_by(RouteLog.id)).all())


# --------------------------------------------------------------------------- #
# 候选清单
# --------------------------------------------------------------------------- #


def test_candidates_follow_configured_priority(session: Session) -> None:
    """顺序由配置的人决定：priority 升序，同 priority 内按 sort_no 稳定排序。"""
    slow = make_provider(session, "slow", priority=200)
    fast = make_provider(session, "fast", priority=10)
    make_model(session, slow, "m-slow", agent_code=AGENT)
    make_model(session, fast, "m-b", agent_code=AGENT, sort_no=2)
    make_model(session, fast, "m-a", agent_code=AGENT, sort_no=1)

    assert [c.label for c in router.candidates_for(session, AGENT)] == [
        "fast/m-a",
        "fast/m-b",
        "slow/m-slow",
    ]


def test_disabled_layers_drop_out_of_candidates(session: Session) -> None:
    off_provider = make_provider(session, "off", priority=1, enabled=False)
    make_model(session, off_provider, "m1", agent_code=AGENT)

    on_provider = make_provider(session, "on", priority=2)
    make_model(session, on_provider, "m-off", agent_code=AGENT, enabled=False)
    make_model(session, on_provider, "m-on", agent_code=AGENT)
    make_model(session, on_provider, "m-unbound")

    assert [c.label for c in router.candidates_for(session, AGENT)] == ["on/m-on"]


def test_candidate_inherits_driver_and_builds_endpoint(session: Session) -> None:
    provider = make_provider(session, "bailian", driver="openai_compat")
    model = make_model(session, provider, "qwen-image-2.0", agent_code=AGENT)
    model.driver = "dashscope_mm"
    model.api_path = "/api/v1/services/aigc/multimodal-generation/generation"
    session.commit()

    inherited = make_model(session, provider, "qwen3.8", agent_code=AGENT, sort_no=9)
    inherited.api_path = "/compatible-mode/v1"
    session.commit()

    by_model = {c.model_id: c for c in router.candidates_for(session, AGENT)}
    assert by_model["qwen-image-2.0"].driver == "dashscope_mm"
    assert by_model["qwen3.8"].driver == "openai_compat"
    assert by_model["qwen3.8"].endpoint == "https://example.invalid/compatible-mode/v1"


# --------------------------------------------------------------------------- #
# 会话粘性
# --------------------------------------------------------------------------- #


def test_first_turn_binds_and_next_turns_reuse(session: Session) -> None:
    """轮转粒度是会话：首轮绑定，之后每轮都是 sticky_hit，不换供应商。"""
    first = make_provider(session, "first", priority=1)
    make_model(session, first, "m1", agent_code=AGENT)
    make_provider(session, "second", priority=2)

    conversation = _conversation(session)
    bound = router.select_candidate(session, AGENT, conversation_id=conversation.id)
    assert bound.outcome == "bound"
    assert conversation.bound_provider_model_id == bound.candidate.provider_model_id
    assert conversation.bound_at is not None

    for _ in range(3):
        again = router.select_candidate(session, AGENT, conversation_id=conversation.id)
        assert again.outcome == "sticky_hit"
        assert again.candidate.provider_model_id == bound.candidate.provider_model_id

    assert conversation.rebind_count == 0
    assert [log.outcome for log in _logs(session)] == [
        "bound",
        "sticky_hit",
        "sticky_hit",
        "sticky_hit",
    ]


def test_no_conversation_means_plain_selection(session: Session) -> None:
    """单次调用型 Agent 没有前缀可复用，按调用选路，不写绑定。"""
    provider = make_provider(session, "only", priority=1)
    make_model(session, provider, "m1", agent_code=AGENT)

    decision = router.select_candidate(session, AGENT)
    assert decision.outcome == "selected"
    assert decision.conversation_id is None


def test_rebind_prefers_the_same_model_on_another_account(session: Session) -> None:
    """换绑先找同 model_id 的其他账号：行为一致，只损失前缀缓存。"""
    primary = make_provider(session, "primary", priority=1)
    other = make_provider(session, "other", priority=2)
    backup = make_provider(session, "backup", priority=3)

    bound_model = make_model(session, primary, "glm-5.3", agent_code=AGENT)
    make_model(session, backup, "glm-5.3", agent_code=AGENT)
    make_model(session, other, "minimax-m3", agent_code=AGENT)

    conversation = _conversation(session)
    router.select_candidate(session, AGENT, conversation_id=conversation.id)
    assert conversation.bound_provider_model_id == bound_model.id

    router.open_breaker(session, bound_model.id, "连不上")
    rebound = router.select_candidate(session, AGENT, conversation_id=conversation.id)

    # other 的 priority 更小，但同模型的 backup 优先——换账号不改行为
    assert rebound.outcome == "rebound"
    assert rebound.candidate.label == "backup/glm-5.3"
    assert conversation.rebind_count == 1
    assert "熔断" in (conversation.rebind_reason or "")


def test_rebind_falls_back_to_another_model(session: Session) -> None:
    """同模型没有别的账号了，才换模型。"""
    primary = make_provider(session, "primary", priority=1)
    fallback = make_provider(session, "fallback", priority=2)
    bound_model = make_model(session, primary, "glm-5.3", agent_code=AGENT)
    make_model(session, fallback, "minimax-m3", agent_code=AGENT)

    conversation = _conversation(session)
    router.select_candidate(session, AGENT, conversation_id=conversation.id)
    router.open_breaker(session, bound_model.id, "连不上")

    rebound = router.select_candidate(session, AGENT, conversation_id=conversation.id)
    assert rebound.candidate.label == "fallback/minimax-m3"
    assert rebound.outcome == "rebound"


def test_binding_lost_with_the_model_rebinds_silently(session: Session) -> None:
    """模型被删只是置空绑定，会话照常继续，下一轮重选。"""
    primary = make_provider(session, "primary", priority=1)
    backup = make_provider(session, "backup", priority=2)
    bound_model = make_model(session, primary, "glm-5.3", agent_code=AGENT)
    make_model(session, backup, "glm-5.3", agent_code=AGENT)

    conversation = _conversation(session)
    router.select_candidate(session, AGENT, conversation_id=conversation.id)

    session.delete(bound_model)
    session.commit()
    session.refresh(conversation)
    assert conversation.bound_provider_model_id is None

    again = router.select_candidate(session, AGENT, conversation_id=conversation.id)
    assert again.outcome == "bound"
    assert again.candidate.label == "backup/glm-5.3"


def test_exhausted_quota_forces_rebind(session: Session) -> None:
    primary = make_provider(session, "primary", priority=1)
    backup = make_provider(session, "backup", priority=2)
    bound_model = make_model(
        session, primary, "glm-5.3", agent_code=AGENT, limit=("tokens", 100, "day")
    )
    make_model(session, backup, "glm-5.3", agent_code=AGENT)

    conversation = _conversation(session)
    router.select_candidate(session, AGENT, conversation_id=conversation.id)
    usage.mark_exhausted(session, bound_model, "tokens")

    rebound = router.select_candidate(session, AGENT, conversation_id=conversation.id)
    assert rebound.candidate.label == "backup/glm-5.3"
    assert "额度" in (conversation.rebind_reason or "")


# --------------------------------------------------------------------------- #
# 熔断
# --------------------------------------------------------------------------- #


def test_breaker_expires_and_cleans_itself(session: Session) -> None:
    provider = make_provider(session, "p1")
    model = make_model(session, provider, "m1", agent_code=AGENT)

    breaker = router.open_breaker(session, model.id, "500")
    assert router.is_open(session, model.id)

    breaker.open_until = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    assert not router.is_open(session, model.id)
    assert session.scalars(select(CircuitBreaker)).one_or_none() is None


def test_open_breaker_accumulates_fail_count(session: Session) -> None:
    provider = make_provider(session, "p1")
    model = make_model(session, provider, "m1", agent_code=AGENT)

    router.open_breaker(session, model.id, "第一次")
    breaker = router.open_breaker(session, model.id, "第二次")
    assert breaker.fail_count == 2
    assert breaker.last_reason == "第二次"


# --------------------------------------------------------------------------- #
# 无候选
# --------------------------------------------------------------------------- #


def test_no_binding_at_all_is_rejected(session: Session) -> None:
    with pytest.raises(NoCandidateError):
        router.select_candidate(session, AGENT)
    logs = _logs(session)
    assert [log.outcome for log in logs] == ["rejected"]
    assert logs[0].provider_code is None


def test_all_candidates_blocked_records_every_reason(session: Session) -> None:
    provider = make_provider(session, "p1")
    broken = make_model(session, provider, "m-broken", agent_code=AGENT)
    drained = make_model(
        session, provider, "m-drained", agent_code=AGENT, sort_no=1, limit=("tokens", 10, "day")
    )
    router.open_breaker(session, broken.id, "连不上")
    usage.mark_exhausted(session, drained, "tokens")

    with pytest.raises(NoCandidateError):
        router.select_candidate(session, AGENT)

    reason = _logs(session)[-1].reason or ""
    assert "熔断" in reason and "额度" in reason


# --------------------------------------------------------------------------- #
# 调用结果回收
# --------------------------------------------------------------------------- #


def test_success_records_tokens_and_clears_breaker(session: Session) -> None:
    provider = make_provider(session, "p1")
    model = make_model(session, provider, "m1", agent_code=AGENT, limit=("tokens", 1000, "day"))
    decision = router.select_candidate(session, AGENT)
    router.open_breaker(session, model.id, "上一次失败")

    router.report_success(
        session, AGENT, decision, CallOutcome(limit_kind="tokens", used_delta=250, latency_ms=88)
    )

    assert not router.is_open(session, model.id)
    assert usage.peek(session, model, "tokens").used == 250
    last = _logs(session)[-1]
    assert last.outcome == "succeeded"
    assert last.used_delta == 250
    assert last.latency_ms == 88


def test_success_prefers_reported_remaining(session: Session) -> None:
    provider = make_provider(session, "p1")
    model = make_model(session, provider, "m1", agent_code=AGENT, limit=("tokens", 1000, "day"))
    decision = router.select_candidate(session, AGENT)

    router.report_success(
        session, AGENT, decision, CallOutcome(limit_kind="tokens", used_delta=10, remaining=42)
    )
    assert usage.peek(session, model, "tokens").remaining == 42


def test_pre_deducted_kinds_are_not_recorded_twice(session: Session) -> None:
    """calls 在选路时已预扣，回收时不能再记一遍。"""
    provider = make_provider(session, "p1")
    model = make_model(session, provider, "m1", agent_code=AGENT, limit=("calls", 5, "day"))

    decision = router.select_candidate(session, AGENT, limit_kind="calls")
    assert usage.peek(session, model, "calls").used == 1

    router.report_success(session, AGENT, decision, CallOutcome(limit_kind="calls", used_delta=1))
    assert usage.peek(session, model, "calls").used == 1


def test_quota_failure_marks_exhausted_without_breaking(session: Session) -> None:
    """额度用尽是账务问题不是故障：标满额度，不熔断。"""
    provider = make_provider(session, "p1")
    model = make_model(session, provider, "m1", agent_code=AGENT, limit=("tokens", 1000, "day"))
    decision = router.select_candidate(session, AGENT)

    router.report_failure(session, AGENT, decision, QuotaExhausted("HTTP 402 欠费"))

    assert not router.is_open(session, model.id)
    assert not usage.has_budget(session, model, "tokens")
    assert _logs(session)[-1].outcome == "failed"


def test_hard_failure_opens_the_breaker(session: Session) -> None:
    provider = make_provider(session, "p1")
    model = make_model(session, provider, "m1", agent_code=AGENT)
    decision = router.select_candidate(session, AGENT)

    router.report_failure(session, AGENT, decision, ProviderError("HTTP 500"))
    assert router.is_open(session, model.id)


def test_rate_limit_does_not_open_the_breaker(session: Session) -> None:
    """限流退避重试还有救，熔断反而把好候选关掉。"""
    provider = make_provider(session, "p1")
    model = make_model(session, provider, "m1", agent_code=AGENT)
    decision = router.select_candidate(session, AGENT)

    router.report_failure(session, AGENT, decision, RetryableError("HTTP 429 限流"))
    assert not router.is_open(session, model.id)


def test_retry_attempts_leave_a_trail(session: Session) -> None:
    provider = make_provider(session, "p1")
    make_model(session, provider, "m1", agent_code=AGENT)
    decision = router.select_candidate(session, AGENT)

    router.note_retryable(session, AGENT, decision, RetryableError("HTTP 429"), attempt_no=2)
    last = _logs(session)[-1]
    assert last.outcome == "retrying"
    assert last.attempt_no == 2


# --------------------------------------------------------------------------- #
# 积分预扣
# --------------------------------------------------------------------------- #


def test_credit_need_reads_the_operation_price(session: Session) -> None:
    provider = make_provider(session, "meshy", driver="meshy")
    make_model(
        session,
        provider,
        "meshy-5",
        agent_code="model3d",
        params={"credit_costs": {"image_to_3d": 5}},
        limit=("credits", 12, "total"),
    )
    candidate = router.candidates_for(session, "model3d")[0]

    assert router.credit_need(candidate, "image_to_3d") == 5
    assert router.credit_need(candidate, "animate") == 1  # 未配单价按 1 扣，不当不限量
    assert router.credit_need(candidate, None) == 1


def test_credits_run_out_after_configured_total(session: Session) -> None:
    """总积分配在 model_limits，扣到不够就没有候选了。"""
    provider = make_provider(session, "meshy", driver="meshy")
    make_model(
        session,
        provider,
        "meshy-5",
        agent_code="model3d",
        params={"credit_costs": {"image_to_3d": 5}},
        limit=("credits", 12, "total"),
    )

    for _ in range(2):
        router.select_candidate(session, "model3d", limit_kind="credits", operation="image_to_3d")
    with pytest.raises(NoCandidateError):
        router.select_candidate(session, "model3d", limit_kind="credits", operation="image_to_3d")


def test_route_log_never_stores_the_api_key(session: Session) -> None:
    provider = make_provider(session, "p1", api_key="sk-sp-secret-value")
    make_model(session, provider, "m1", agent_code=AGENT)
    router.select_candidate(session, AGENT)

    for log in _logs(session):
        assert "secret" not in (log.reason or "")
        assert "secret" not in (log.provider_code or "")
