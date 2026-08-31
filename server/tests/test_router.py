"""选路：会话粘性、严格 priority、换绑优先同模型、熔断与额度闸门、route_logs。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from atelier.db.project_models import Conversation
from atelier.db.runtime_models import CircuitBreaker, ModelLimit, ProviderModel, RouteLog
from atelier.providers import router, usage
from atelier.providers.base import (
    CallOutcome,
    EmptyReply,
    NoCandidateError,
    ProviderError,
    QuotaExhausted,
    RetryableError,
)
from tests.conftest import make_model, make_provider

AGENT = "spec_writer"


def _conversation(session: Session, cid: str = "c1") -> Conversation:
    """会话行住在项目库里，但选路层只按 StickyBinding 协议读写它，不关心它在哪个库。

    所以这里不入库也能测：直接造一个游离对象交给 `select_candidate`，它改完属性由谁提交
    是调用方的事。这正好把「选路不得摸项目库」这个约束钉住了。
    """
    return Conversation(
        id=cid,
        target_kind="character",
        target_ref="chitong_shuangweishou",
        agent_code=AGENT,
        bound_provider_label="",
        rebind_count=0,
    )


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
    bound = router.select_candidate(session, AGENT, binding=conversation)
    assert bound.outcome == "bound"
    assert conversation.bound_provider_model_id == bound.candidate.provider_model_id
    assert conversation.bound_at is not None

    for _ in range(3):
        again = router.select_candidate(session, AGENT, binding=conversation)
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
    router.select_candidate(session, AGENT, binding=conversation)
    assert conversation.bound_provider_model_id == bound_model.id

    router.open_breaker(session, bound_model.id, "连不上")
    rebound = router.select_candidate(session, AGENT, binding=conversation)

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
    router.select_candidate(session, AGENT, binding=conversation)
    router.open_breaker(session, bound_model.id, "连不上")

    rebound = router.select_candidate(session, AGENT, binding=conversation)
    assert rebound.candidate.label == "fallback/minimax-m3"
    assert rebound.outcome == "rebound"


def test_binding_lost_with_the_model_rebinds_silently(session: Session) -> None:
    """模型被删后会话里剩下一个悬空 id，下一轮当「已绑不可用」换绑，会话照常继续。

    跳库了就没有外键 SET NULL 可依赖（会话在项目库、模型在全局库），这正是项目目录换了
    机器后的常态：id 对不上也得能跑。
    """
    primary = make_provider(session, "primary", priority=1)
    backup = make_provider(session, "backup", priority=2)
    bound_model = make_model(session, primary, "glm-5.3", agent_code=AGENT)
    make_model(session, backup, "glm-5.3", agent_code=AGENT)

    conversation = _conversation(session)
    router.select_candidate(session, AGENT, binding=conversation)

    session.delete(bound_model)
    session.commit()

    again = router.select_candidate(session, AGENT, binding=conversation)
    assert again.outcome == "rebound"
    assert again.candidate.label == "backup/glm-5.3"
    assert "已绑模型" in (conversation.rebind_reason or "")


def test_exhausted_quota_forces_rebind(session: Session) -> None:
    primary = make_provider(session, "primary", priority=1)
    backup = make_provider(session, "backup", priority=2)
    bound_model = make_model(
        session, primary, "glm-5.3", agent_code=AGENT, limit=("tokens", 100, "day")
    )
    make_model(session, backup, "glm-5.3", agent_code=AGENT)

    conversation = _conversation(session)
    router.select_candidate(session, AGENT, binding=conversation)
    usage.mark_exhausted(session, bound_model, "tokens")

    rebound = router.select_candidate(session, AGENT, binding=conversation)
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


def test_empty_reply_does_not_open_the_breaker(session: Session) -> None:
    """模型本身是通的，只是这一句没说出来；关它几分钟会连带下一句正常提问也发不出去。"""
    provider = make_provider(session, "p1")
    model = make_model(session, provider, "m1", agent_code=AGENT)
    decision = router.select_candidate(session, AGENT)

    router.report_failure(session, AGENT, decision, EmptyReply("p1/m1 返回了空回答"))

    assert not router.is_open(session, model.id)
    # 不熔断不等于不记账：后面排查「这个模型老不说话」靠的就是这条
    assert _logs(session)[-1].outcome == "failed"


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


# --------------------------------------------------------------------------- #
# 出图张数
# --------------------------------------------------------------------------- #


def _picture_model(session: Session, *, calls: int, images: int) -> ProviderModel:
    """一个既按接口次数、又按出图张数记账的图片模型。"""
    provider = make_provider(session, "ark")
    model = make_model(
        session, provider, "seedream-5.0", agent_code=AGENT, limit=("calls", calls, "day")
    )
    session.add(
        ModelLimit(
            provider_model_id=model.id, limit_kind="images", max_value=images, period_expr="day"
        )
    )
    session.commit()
    return model


def test_image_kind_is_deducted_alongside_calls(session: Session) -> None:
    """生图的主口径还是 calls，images 是附加口径，两笔账各记各的。"""
    model = _picture_model(session, calls=10, images=3)

    router.select_candidate(session, AGENT, limit_kind="calls", also_kinds=("images",))

    assert usage.peek(session, model, "calls").used == 1
    assert usage.peek(session, model, "images").used == 1


def test_units_decide_how_many_pictures_are_deducted(session: Session) -> None:
    """一次出四张就扣四张：只按 calls 算的话，四倍的消耗会被记成一次。"""
    model = _picture_model(session, calls=10, images=8)

    router.select_candidate(session, AGENT, limit_kind="calls", also_kinds=("images",), units=4)

    assert usage.peek(session, model, "calls").used == 1
    assert usage.peek(session, model, "images").used == 4


def test_pictures_run_out_before_calls_do(session: Session) -> None:
    """张数先见底就该没候选了，哪怕接口次数还剩一大把。"""
    model = _picture_model(session, calls=10, images=2)

    for _ in range(2):
        router.select_candidate(session, AGENT, limit_kind="calls", also_kinds=("images",))
    with pytest.raises(NoCandidateError):
        router.select_candidate(session, AGENT, limit_kind="calls", also_kinds=("images",))

    assert "images" in (_logs(session)[-1].reason or "")
    # 张数不够就一笔都别扣：次数被白扣掉的话，这个窗口内的账就永远对不上了
    assert usage.peek(session, model, "calls").used == 2


def test_model_without_an_image_limit_is_never_blocked(session: Session) -> None:
    """没配张数限额就是不限张数，附加口径不能凭空拦人。"""
    provider = make_provider(session, "ark")
    model = make_model(session, provider, "seedream-5.0", agent_code=AGENT)

    for _ in range(3):
        router.select_candidate(session, AGENT, limit_kind="calls", also_kinds=("images",))

    assert usage.peek(session, model, "images").unlimited


def test_route_log_never_stores_the_api_key(session: Session) -> None:
    provider = make_provider(session, "p1", api_key="sk-sp-secret-value")
    make_model(session, provider, "m1", agent_code=AGENT)
    router.select_candidate(session, AGENT)

    for log in _logs(session):
        assert "secret" not in (log.reason or "")
        assert "secret" not in (log.provider_code or "")
