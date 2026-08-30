"""选路：会话级粘性绑定 + 严格 priority 顺序 + 熔断 + 额度闸门 + route_logs。

两条铁律：

1. **轮转粒度是会话，不是调用**。多轮对话每轮都要重发前缀，换 provider 等于让对方从零
   算一遍前缀、自己这边的缓存全部作废。所以会话首轮绑定后就一直用它，只有熔断、额度
   用尽或该模型被删才换绑；换绑先在**同 model_id 的其他账号**里找（行为一致，只损失
   缓存），找不到才换模型。`conversational=false` 的 Agent 没有前缀可复用，按调用选路。
2. **顺序由配置的人决定**，路由层不打分、不做均衡：严格按 provider.priority 升序，同
   priority 内按 sort_no、provider_code、model_id 稳定排序，第一个可用的就是它。

额度口径分两种：tokens 只能「调用前查余量、调用后补记」（`report_success`），
calls / credits / images 在调用前就知道消耗，选中即预扣（Meshy 的每种操作单价配在
`provider_models.params.credit_costs`）。一次调用可以同时卡几种口径：生图既算接口次数（calls）
也算出图张数（images），哪种先满那个候选就不能用了。

这里不发一个业务 HTTP 请求：驱动实现随 A4、A9 落，只需把 `CallOutcome` 交回来记账。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

from atelier.db.runtime_models import (
    CircuitBreaker,
    Provider,
    ProviderAgentModel,
    ProviderModel,
    RouteLog,
)
from atelier.providers import usage
from atelier.providers.base import (
    CallOutcome,
    Candidate,
    Decision,
    NoCandidateError,
    ProviderError,
    QuotaExhausted,
    RetryableError,
    StickyBinding,
)
from atelier.settings import get_settings

_log = structlog.get_logger(__name__)

# 预扣型口径：消耗在调用前已知，选中即扣
PRE_DEDUCT_KINDS = ("calls", "credits", "images")


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


# --------------------------------------------------------------------------- #
# 候选清单
# --------------------------------------------------------------------------- #


def candidates_for(session: Session, agent_code: str) -> list[Candidate]:
    """该 Agent 的候选，按 priority 升序。禁用的 provider、模型、绑定一律不出现。"""
    rows = session.execute(
        sa_select(ProviderModel, Provider)
        .join(ProviderAgentModel, ProviderAgentModel.provider_model_id == ProviderModel.id)
        .join(Provider, Provider.code == ProviderModel.provider_code)
        .where(
            ProviderAgentModel.agent_code == agent_code,
            ProviderAgentModel.enabled.is_(True),
            ProviderModel.enabled.is_(True),
            Provider.enabled.is_(True),
        )
    ).all()

    candidates = [_to_candidate(pm, provider) for pm, provider in rows]
    candidates.sort(key=lambda c: c.sort_key())
    return candidates


def _to_candidate(provider_model: ProviderModel, provider: Provider) -> Candidate:
    return Candidate(
        provider_model_id=provider_model.id,
        provider_code=provider.code,
        provider_name=provider.name,
        model_id=provider_model.model_id,
        driver=provider_model.driver or provider.driver,
        endpoint=provider_model.endpoint(),
        api_key=provider.api_key,
        priority=provider.priority,
        sort_no=provider_model.sort_no,
        verify_ssl=provider.verify_ssl,
        auth_style=provider.auth_style,
        params=dict(provider_model.params or {}),
    )


def credit_need(candidate: Candidate, operation: str | None) -> int:
    """该操作的预扣额，未配单价按 1 算（至少扣一次，避免不限量误判）。"""
    costs = candidate.params.get("credit_costs")
    if operation and isinstance(costs, dict):
        try:
            return max(int(costs.get(operation, 0)), 0) or 1
        except (TypeError, ValueError):
            return 1
    return 1


def need_of(candidate: Candidate, limit_kind: str, operation: str | None, units: int) -> int:
    """这一次要扣多少：credits 看操作单价，images 看出几张，其余口径一次算一笔。"""
    if limit_kind == "credits":
        return credit_need(candidate, operation)
    if limit_kind == "images":
        return max(int(units or 1), 1)
    return 1


# --------------------------------------------------------------------------- #
# 熔断
# --------------------------------------------------------------------------- #


def breaker_of(session: Session, provider_model_id: int) -> CircuitBreaker | None:
    return session.scalars(
        sa_select(CircuitBreaker).where(CircuitBreaker.provider_model_id == provider_model_id)
    ).one_or_none()


def is_open(session: Session, provider_model_id: int, now: datetime | None = None) -> bool:
    """熔断窗口未过即跳过该候选。到期的记录顺手删掉，不留脏数据。"""
    breaker = breaker_of(session, provider_model_id)
    if breaker is None:
        return False
    if _as_utc(breaker.open_until) > (now or _now()):
        return True
    session.execute(delete(CircuitBreaker).where(CircuitBreaker.id == breaker.id))
    session.commit()
    return False


def open_breaker(session: Session, provider_model_id: int, reason: str) -> CircuitBreaker:
    """打开或延长熔断窗口，失败次数累加。"""
    seconds = get_settings().circuit_breaker_seconds
    breaker = breaker_of(session, provider_model_id)
    if breaker is None:
        breaker = CircuitBreaker(
            provider_model_id=provider_model_id, fail_count=0, open_until=_now()
        )
        session.add(breaker)
    breaker.fail_count += 1
    breaker.open_until = _now() + timedelta(seconds=seconds)
    breaker.last_reason = reason[:500]
    session.commit()
    return breaker


def close_breaker(session: Session, provider_model_id: int) -> None:
    """调用成功即清除熔断记录。"""
    session.execute(
        delete(CircuitBreaker).where(CircuitBreaker.provider_model_id == provider_model_id)
    )
    session.commit()


# --------------------------------------------------------------------------- #
# 选路
# --------------------------------------------------------------------------- #


def select_candidate(
    session: Session,
    agent_code: str,
    *,
    binding: StickyBinding | None = None,
    conversation_id: str | None = None,
    limit_kind: str = "tokens",
    also_kinds: Sequence[str] = (),
    units: int = 1,
    operation: str | None = None,
    task_id: str | None = None,
    project_code: str | None = None,
) -> Decision:
    """选出这次要用的候选，写一条 route_log；一个都没有就抛 NoCandidateError。

    给了 binding（会话行）就走粘性：已绑且仍可用就直接复用，不动任何额度判定之外的状态。
    绑定的变更只写在传进来的对象上，**由调用方提交项目库**；`session` 是全局库，提交它
    落不了会话的改动。

    limit_kind 是记账用的主口径（写进 route_log），also_kinds 是同一次调用还要一并卡的口径，
    模型上没配那种限额就自然不生效。
    """
    kinds = _kinds_of(limit_kind, also_kinds)
    conversation_id = conversation_id or (binding.id if binding is not None else None)
    candidates = candidates_for(session, agent_code)
    if not candidates:
        _log_route(
            session,
            agent_code=agent_code,
            candidate=None,
            outcome="rejected",
            reason="没有启用的候选：检查 provider、模型与 Agent 绑定是否都已启用",
            limit_kind=limit_kind,
            task_id=task_id,
            conversation_id=conversation_id,
            project_code=project_code,
        )
        raise NoCandidateError(f"Agent {agent_code} 没有可用候选")

    conversation = binding
    skipped: list[tuple[str, str]] = []

    # 1. 粘性命中：会话已绑定且仍可用
    bound_id = conversation.bound_provider_model_id if conversation is not None else None
    bound = next((c for c in candidates if c.provider_model_id == bound_id), None)
    if bound_id is not None:
        if bound is None:
            skipped.append(
                (f"provider_model#{bound_id}", "已绑模型被删、被禁用或不再挂在该 Agent 下")
            )
        else:
            blocked = _blocked_reason(session, bound, kinds, operation, units, reserve=False)
            if blocked is None:
                decision = Decision(
                    candidate=bound,
                    outcome="sticky_hit",
                    conversation_id=conversation_id,
                    skipped=(),
                )
                _reserve_if_needed(session, bound, kinds, operation, units)
                _log_decision(session, agent_code, decision, task_id, project_code, limit_kind)
                return decision
            skipped.append((bound.label, blocked))

    # 2. 换绑或首次选路：换绑优先同 model_id 的其他账号，只丢缓存不改行为
    pool = [c for c in candidates if c.provider_model_id != bound_id]
    if bound is not None:
        same_model = [c for c in pool if c.model_id == bound.model_id]
        pool = same_model + [c for c in pool if c.model_id != bound.model_id]

    for candidate in pool:
        blocked = _blocked_reason(session, candidate, kinds, operation, units, reserve=True)
        if blocked is not None:
            skipped.append((candidate.label, blocked))
            continue

        if conversation is None:
            outcome, reason = "selected", None
        elif bound_id is None:
            outcome, reason = "bound", None
        else:
            outcome = "rebound"
            reason = skipped[0][1] if skipped else "原绑定不可用"
            conversation.rebind_count += 1
            conversation.rebind_reason = reason[:255]

        if conversation is not None:
            conversation.bound_provider_model_id = candidate.provider_model_id
            conversation.bound_provider_label = candidate.label[:255]
            conversation.bound_at = _now()
            # 会话在项目库里，这边 commit 不了它——留给调用方提交自己那个库

        decision = Decision(
            candidate=candidate,
            outcome=outcome,
            reason=reason,
            conversation_id=conversation_id,
            skipped=tuple(skipped),
        )
        _log_decision(session, agent_code, decision, task_id, project_code, limit_kind)
        return decision

    reason = "；".join(f"{label}: {why}" for label, why in skipped) or "全部候选不可用"
    _log_route(
        session,
        agent_code=agent_code,
        candidate=None,
        outcome="rejected",
        reason=reason,
        limit_kind=limit_kind,
        task_id=task_id,
        conversation_id=conversation_id,
        project_code=project_code,
    )
    raise NoCandidateError(f"Agent {agent_code} 全部候选不可用：{reason}")


def _kinds_of(limit_kind: str, also_kinds: Sequence[str]) -> tuple[str, ...]:
    """主口径排头一份去重名单，同一种口径不能扣两遍。"""
    ordered = [limit_kind, *also_kinds]
    seen: list[str] = []
    for kind in ordered:
        if kind and kind not in seen:
            seen.append(kind)
    return tuple(seen)


def _blocked_reason(
    session: Session,
    candidate: Candidate,
    kinds: tuple[str, ...],
    operation: str | None,
    units: int,
    *,
    reserve: bool,
) -> str | None:
    """候选为什么不能用，能用返回 None。

    reserve=True 时对预扣型口径当场扣额（扣不动就是没额度）；粘性命中路径传 False，
    预扣留到确认复用之后再做，避免白扣。
    """
    if is_open(session, candidate.provider_model_id):
        breaker = breaker_of(session, candidate.provider_model_id)
        return f"熔断中（{breaker.last_reason or '未记录原因'}）" if breaker else "熔断中"

    provider_model = session.get(ProviderModel, candidate.provider_model_id)
    if provider_model is None:
        return "模型记录已删除"

    # 先把所有口径都查一遍，再动手扣：先扣后发现另一种不够，先那一笔就白扣了
    for kind in kinds:
        need = need_of(candidate, kind, operation, units)
        if not usage.has_budget(session, provider_model, kind, need=need):
            return f"{kind} 额度本窗口已用尽"

    if reserve:
        for kind in kinds:
            if kind not in PRE_DEDUCT_KINDS:
                continue
            need = need_of(candidate, kind, operation, units)
            budget = usage.reserve(session, provider_model, kind, delta=need)
            if not budget.granted:
                return f"{kind} 预扣失败（{budget.line()}）"
    return None


def _reserve_if_needed(
    session: Session,
    candidate: Candidate,
    kinds: tuple[str, ...],
    operation: str | None,
    units: int,
) -> None:
    provider_model = session.get(ProviderModel, candidate.provider_model_id)
    if provider_model is None:
        return
    for kind in kinds:
        if kind not in PRE_DEDUCT_KINDS:
            continue
        usage.reserve(
            session, provider_model, kind, delta=need_of(candidate, kind, operation, units)
        )


# --------------------------------------------------------------------------- #
# 调用结果回收
# --------------------------------------------------------------------------- #


def report_success(
    session: Session,
    agent_code: str,
    decision: Decision,
    outcome: CallOutcome,
    *,
    task_id: str | None = None,
    project_code: str | None = None,
) -> None:
    """调用成功：清熔断、补记 token 消耗、落供应商报告的剩余额度。"""
    provider_model = session.get(ProviderModel, decision.candidate.provider_model_id)
    close_breaker(session, decision.candidate.provider_model_id)

    if provider_model is not None:
        if outcome.limit_kind not in PRE_DEDUCT_KINDS and outcome.used_delta:
            usage.record(session, provider_model, outcome.limit_kind, outcome.used_delta)
        if outcome.remaining is not None:
            usage.apply_remaining(session, provider_model, outcome.limit_kind, outcome.remaining)

    _log_route(
        session,
        agent_code=agent_code,
        candidate=decision.candidate,
        outcome="succeeded",
        reason=None,
        limit_kind=outcome.limit_kind,
        used_delta=outcome.used_delta,
        latency_ms=outcome.latency_ms,
        task_id=task_id,
        conversation_id=decision.conversation_id,
        project_code=project_code,
    )


def report_failure(
    session: Session,
    agent_code: str,
    decision: Decision,
    error: ProviderError,
    *,
    limit_kind: str = "tokens",
    task_id: str | None = None,
    project_code: str | None = None,
) -> None:
    """调用失败：额度用尽就标满，其余打开熔断。两种都记 route_log。

    限流（RetryableError）不熔断——退避重试还有救，熔断反而把好候选关掉。
    """
    provider_model = session.get(ProviderModel, decision.candidate.provider_model_id)
    reason = str(error)

    if isinstance(error, QuotaExhausted):
        if provider_model is not None:
            usage.mark_exhausted(session, provider_model, limit_kind)
    elif not isinstance(error, RetryableError):
        open_breaker(session, decision.candidate.provider_model_id, reason)

    _log_route(
        session,
        agent_code=agent_code,
        candidate=decision.candidate,
        outcome="failed",
        reason=reason,
        limit_kind=limit_kind,
        task_id=task_id,
        conversation_id=decision.conversation_id,
        project_code=project_code,
    )


def note_retryable(
    session: Session, agent_code: str, decision: Decision, error: RetryableError, attempt_no: int
) -> None:
    """重试用尽前的每次限流都留痕，便于回看是谁在限流。"""
    _log_route(
        session,
        agent_code=agent_code,
        candidate=decision.candidate,
        outcome="retrying",
        reason=str(error),
        attempt_no=attempt_no,
        conversation_id=decision.conversation_id,
    )


# --------------------------------------------------------------------------- #
# route_logs
# --------------------------------------------------------------------------- #


def _log_decision(
    session: Session,
    agent_code: str,
    decision: Decision,
    task_id: str | None,
    project_code: str | None,
    limit_kind: str,
) -> None:
    reason = decision.reason
    if decision.skipped and decision.outcome != "sticky_hit":
        skipped_text = "；".join(f"{label}: {why}" for label, why in decision.skipped)
        reason = f"{reason}｜跳过 {skipped_text}" if reason else f"跳过 {skipped_text}"
    _log_route(
        session,
        agent_code=agent_code,
        candidate=decision.candidate,
        outcome=decision.outcome,
        reason=reason,
        limit_kind=limit_kind,
        task_id=task_id,
        conversation_id=decision.conversation_id,
        project_code=project_code,
    )


def _log_route(
    session: Session,
    *,
    agent_code: str,
    candidate: Candidate | None,
    outcome: str,
    reason: str | None,
    limit_kind: str | None = None,
    used_delta: int | None = None,
    latency_ms: int | None = None,
    attempt_no: int = 1,
    task_id: str | None = None,
    conversation_id: str | None = None,
    project_code: str | None = None,
) -> None:
    session.add(
        RouteLog(
            agent_code=agent_code,
            provider_code=candidate.provider_code if candidate else None,
            model_id=candidate.model_id if candidate else None,
            outcome=outcome,
            reason=reason,
            limit_kind=limit_kind,
            used_delta=used_delta,
            latency_ms=latency_ms,
            attempt_no=attempt_no,
            task_id=task_id,
            conversation_id=conversation_id,
            project_code=project_code,
        )
    )
    session.commit()
    _log.info(
        "route",
        agent=agent_code,
        candidate=candidate.label if candidate else None,
        outcome=outcome,
        reason=reason,
    )
