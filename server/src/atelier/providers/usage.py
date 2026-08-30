"""额度账务：远程用量服务发许可，本地 `usage_counters` 做镜像与兜底。

同一批 api_key 会被多个工具、多台机器同时用，各记一套账就会把额度合起来打穿。所以
判定与扣减都发给远程用量服务在同一个事务里完成；本表是它的镜像，远程挂掉时接着拦。

判定顺序（与 `tools/basic/api_usage` 一致）：

1. 本地镜像已标满 → 当场拒发，一个远程请求都不打
2. 远程发许可 → 返回值整条覆写本地镜像
3. 远程不可用 → 纯本地「判定 + 自增」

**上限一律以本地 `model_limits.max_value` 为准**：远程存的 limit 只是上一次记账时的
快照，上限调大后它还是旧值，照抄会把新额度按回旧上限。

记账时机分两种：

- `reserve()` 调用前预扣：消耗在调用前已知（生图按次、Meshy 按操作单价）
- `has_budget()` + `record()` 调用前查余量、调用后补记：token 只有响应回来才知道用了
  多少。代价是最多被最后一次调用略微打穿，之后本窗口直接停用该候选
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from atelier.db.runtime_models import ModelLimit, ProviderModel, UsageCounter
from atelier.providers import period as period_mod
from atelier.providers.usage_client import Permit, get_usage_client, key_id_of, service_of
from atelier.settings import get_settings

_log = structlog.get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Budget:
    """某个候选在当前窗口的额度状况。limit=0 表示未配额度，不统计也不拦截。"""

    limit_kind: str
    limit: int
    used: int
    window_key: str
    source: str
    granted: bool = True
    remaining: int | None = None
    exhausted_at: datetime | None = None

    @property
    def unlimited(self) -> bool:
        return self.limit <= 0

    @property
    def available(self) -> int | None:
        """还能用多少；未配额度返回 None。"""
        if self.unlimited:
            return None
        if self.remaining is not None:
            return max(self.remaining, 0)
        return max(self.limit - self.used, 0)

    def line(self) -> str:
        if self.unlimited:
            return f"{self.limit_kind} 未配额度"
        return f"{self.limit_kind} {self.used}/{self.limit}（{self.window_key}，{self.source}）"


def _unlimited(limit_kind: str, window_key: str = "-") -> Budget:
    return Budget(
        limit_kind=limit_kind,
        limit=0,
        used=0,
        window_key=window_key,
        source="unlimited",
        granted=True,
    )


def limit_of(session: Session, provider_model: ProviderModel, limit_kind: str) -> ModelLimit | None:
    """该候选在这个计量口径上的额度配置，未配返回 None（无限额）。"""
    return session.scalars(
        select(ModelLimit).where(
            ModelLimit.provider_model_id == provider_model.id,
            ModelLimit.limit_kind == limit_kind,
        )
    ).one_or_none()


def _counter(
    session: Session, provider_model: ProviderModel, limit_kind: str, window_key: str
) -> UsageCounter:
    """取或建当前窗口的计量行。旧窗口的行留着不动，作为历史看板。"""
    row = session.scalars(
        select(UsageCounter).where(
            UsageCounter.provider_model_id == provider_model.id,
            UsageCounter.limit_kind == limit_kind,
            UsageCounter.window_key == window_key,
        )
    ).one_or_none()
    if row is None:
        row = UsageCounter(
            provider_model_id=provider_model.id,
            limit_kind=limit_kind,
            window_key=window_key,
            used_value=0,
        )
        session.add(row)
        session.flush()
    return row


def _mirror(row: UsageCounter, permit: Permit, source: str) -> None:
    """把远程口径整条覆写到本地镜像。"""
    row.used_value = permit.used
    row.remaining_value = permit.remaining
    row.source = source
    if not permit.granted:
        row.exhausted_at = row.exhausted_at or _now()


def _budget(row: UsageCounter, limit: int, granted: bool) -> Budget:
    return Budget(
        limit_kind=row.limit_kind,
        limit=limit,
        used=row.used_value,
        window_key=row.window_key,
        source=row.source,
        granted=granted,
        remaining=row.remaining_value,
        exhausted_at=row.exhausted_at,
    )


def peek(session: Session, provider_model: ProviderModel, limit_kind: str) -> Budget:
    """只读当前窗口用量，**不扣任何额度**。

    远程可用时以远程口径覆写本地镜像；limit 始终取本地配置。
    """
    limit_row = limit_of(session, provider_model, limit_kind)
    if limit_row is None or limit_row.max_value <= 0:
        return _unlimited(limit_kind)

    window = period_mod.window_label(limit_row.period_expr)
    row = _counter(session, provider_model, limit_kind, window)

    client = get_usage_client()
    service = service_of(provider_model.provider_code, provider_model.model_id)
    items = client.snapshot(service, limit_row.period_expr)
    if items is not None:
        key_id = key_id_of(provider_model.provider.api_key)
        for item in items:
            if not isinstance(item, dict) or str(item.get("keyId") or "") != key_id:
                continue
            if str(item.get("limitKey") or "") != window:
                break  # 远程记的是上一个窗口，当前窗口就是 0
            try:
                row.used_value = int(item.get("quta") or 0)
            except (TypeError, ValueError):
                break
            row.source = "remote"
            break

    session.commit()
    return _budget(row, limit_row.max_value, granted=row.exhausted_at is None)


def local_peek(session: Session, provider_model: ProviderModel, limit_kind: str) -> Budget:
    """只读本地镜像，不问远程。

    额度看板一次要看几十个候选，逐个打远程会把一次刷新拖成几十个往返；镜像里的数是
    上一次调用留下的，看板够用，要精确数字点刷新走 `peek`。
    """
    limit_row = limit_of(session, provider_model, limit_kind)
    if limit_row is None or limit_row.max_value <= 0:
        return _unlimited(limit_kind)

    row = _counter(
        session, provider_model, limit_kind, period_mod.window_label(limit_row.period_expr)
    )
    session.commit()
    return _budget(row, limit_row.max_value, granted=row.exhausted_at is None)


def has_budget(
    session: Session, provider_model: ProviderModel, limit_kind: str, need: int = 1
) -> bool:
    """当前窗口还装得下 need 吗；未配额度恒为 True。

    严格口径：装不下就是停用，不做任何兜底放行。要么把 max_value 调大，要么再加一个
    provider。
    """
    seen = peek(session, provider_model, limit_kind)
    if seen.unlimited:
        return True
    if seen.exhausted_at is not None:
        return False

    available = seen.available or 0
    if available < max(need, 1):
        return False

    warn_at = int(seen.limit * get_settings().usage_warn_ratio)
    if seen.used >= warn_at:
        _log.warning(
            "额度即将用完",
            provider=provider_model.provider_code,
            model=provider_model.model_id,
            usage=seen.line(),
        )
    return True


def reserve(
    session: Session, provider_model: ProviderModel, limit_kind: str, delta: int = 1
) -> Budget:
    """调用前预扣 delta；granted=False 即额度不够，换下一个候选。

    用于消耗在调用前已知的口径：生图按次、Meshy 按操作单价。
    """
    return _acquire(session, provider_model, limit_kind, delta=max(int(delta or 0), 0))


def record(session: Session, provider_model: ProviderModel, limit_kind: str, delta: int) -> Budget:
    """调用后补记真实消耗。

    拒发即视为额度到顶：直接标满，让其他机器与后续调用在本窗口内都跳过这个候选。
    """
    used = max(int(delta or 0), 0)
    if not used:
        return _unlimited(limit_kind)
    result = _acquire(session, provider_model, limit_kind, delta=used)
    if not result.granted and not result.unlimited:
        return mark_exhausted(session, provider_model, limit_kind)
    return result


def mark_exhausted(session: Session, provider_model: ProviderModel, limit_kind: str) -> Budget:
    """把当前窗口直接标成用满——接口实报额度用尽时用，官方口径比本地计数准。"""
    limit_row = limit_of(session, provider_model, limit_kind)
    if limit_row is None or limit_row.max_value <= 0:
        return _unlimited(limit_kind)

    window = period_mod.window_label(limit_row.period_expr)
    row = _counter(session, provider_model, limit_kind, window)

    permit = get_usage_client().acquire(
        service_of(provider_model.provider_code, provider_model.model_id),
        provider_model.provider.api_key,
        limit_row.period_expr,
        limit_row.max_value,
        exhausted=True,
    )
    if permit is not None:
        _mirror(row, permit, "remote")
    else:
        row.source = "local"
    row.used_value = max(row.used_value, limit_row.max_value)
    row.remaining_value = 0
    row.exhausted_at = row.exhausted_at or _now()
    session.commit()

    _log.warning(
        "额度已用尽，本窗口停用该候选",
        provider=provider_model.provider_code,
        model=provider_model.model_id,
        window=window,
    )
    return _budget(row, limit_row.max_value, granted=False)


def apply_remaining(
    session: Session, provider_model: ProviderModel, limit_kind: str, remaining: int
) -> Budget:
    """把供应商响应头报告的剩余额度记进来——官方口径优先于本地累加。

    剩余为 0 即标满，其余只更新 remaining 与 used，不动远程账（远程只认增量）。
    """
    limit_row = limit_of(session, provider_model, limit_kind)
    if limit_row is None or limit_row.max_value <= 0:
        return _unlimited(limit_kind)
    if remaining <= 0:
        return mark_exhausted(session, provider_model, limit_kind)

    window = period_mod.window_label(limit_row.period_expr)
    row = _counter(session, provider_model, limit_kind, window)
    row.remaining_value = remaining
    row.used_value = max(row.used_value, limit_row.max_value - remaining)
    row.source = "header"
    session.commit()
    return _budget(row, limit_row.max_value, granted=True)


def _acquire(
    session: Session, provider_model: ProviderModel, limit_kind: str, delta: int
) -> Budget:
    limit_row = limit_of(session, provider_model, limit_kind)
    if limit_row is None or limit_row.max_value <= 0:
        return _unlimited(limit_kind)

    limit = limit_row.max_value
    window = period_mod.window_label(limit_row.period_expr)
    row = _counter(session, provider_model, limit_kind, window)

    # 1. 本地镜像已标满：用完之后不必再问远程
    if row.exhausted_at is not None:
        session.commit()
        return _budget(row, limit, granted=False)

    # 2. 远程发许可，结果整条覆写本地镜像
    permit = get_usage_client().acquire(
        service_of(provider_model.provider_code, provider_model.model_id),
        provider_model.provider.api_key,
        limit_row.period_expr,
        limit,
        delta=delta,
    )
    if permit is not None:
        _mirror(row, permit, "remote")
        session.commit()
        return _budget(row, limit, granted=permit.granted)

    # 3. 远程不可用：本地判定 + 自增
    granted = row.used_value + delta <= limit
    if granted:
        row.used_value += delta
    else:
        row.exhausted_at = row.exhausted_at or _now()
    row.source = "local"
    session.commit()
    return _budget(row, limit, granted=granted)
