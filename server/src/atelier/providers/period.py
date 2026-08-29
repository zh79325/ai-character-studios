"""额度窗口：把 `period_expr` 算成窗口标签（limitKey）与起止时刻。

窗口边界按**本机时区**算：供应商公布的重置时刻（百炼 Token Plan 每日 11:00、火山按量
每日 11:00）都是当地时间，用户在额度看板上看到的也是当地时间。

`day` / `month` / `day+nH` 三种写法与远程用量服务（api-useage-server 的
`normalize_period`）**同一套语法、同一套标签**，两端各自算窗口也能对上同一条账；
改这三种的算法必须同步改那边。其余写法远程表达不了，只在本地记账：

    day         每天 00:00 起算            标签 2026-08-30
    day+11H     每天 11:00 起算            标签 2026-08-30+11H
    month       每月 1 日 00:00 起算       标签 2026-08
    hour        每小时整点（本地）          标签 2026-08-30T14
    week        每周一 00:00（本地）        标签 2026-W35
    total       永不重置，用于买断式积分池   标签 total
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# 与远程用量服务共用语法的写法，只有这些能把账记到远程
REMOTE_UNITS = ("day", "month")
# 本地专用：远程的 normalize_period 表达不了，落到本地 usage_counters
LOCAL_UNITS = ("hour", "week", "total")

TOTAL = "total"

_EXPR_RE = re.compile(r"\A(?P<unit>hour|day|week|month|total)(?:\+(?P<offset>\d{1,2})H)?\Z")


class PeriodExprError(ValueError):
    """period_expr 写法非法。"""


@dataclass(frozen=True, slots=True)
class Window:
    """一个额度窗口，左闭右开。start / end 为 UTC，end 为 None 表示永不重置。"""

    label: str
    expr: str
    start: datetime | None
    end: datetime | None

    def contains(self, moment: datetime) -> bool:
        if self.start is None:
            return True
        if self.end is None:
            return _as_utc(moment) >= self.start
        return self.start <= _as_utc(moment) < self.end


def _as_utc(moment: datetime) -> datetime:
    """naive 视作 UTC；aware 统一换算到 UTC。库里读回的是 naive。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _parse(expr: str) -> tuple[str, int]:
    match = _EXPR_RE.match(str(expr or "").strip())
    if match is None:
        raise PeriodExprError(
            f"period_expr={expr!r} 非法，支持 day / day+nH / month / hour / week / total"
        )
    unit = match.group("unit")
    offset = int(match.group("offset") or 0)
    if offset > 23:
        raise PeriodExprError(f"period_expr={expr!r} 的起算时刻必须在 0-23")
    if offset and unit != "day":
        raise PeriodExprError(f"period_expr={expr!r}：只有 day 支持 +nH 起算时刻")
    return unit, offset


def normalize(expr: str) -> str:
    """归一写法，非法即报错。

    `day+09H` 与 `day+9H` 必须归到同一个值，否则两种写法算出两个标签，
    同一份额度被记成两条账。
    """
    unit, offset = _parse(expr)
    return f"day+{offset}H" if offset else unit


def day_offset_of(expr: str) -> int | None:
    """`day+nH` 返回起算小时 n，其余返回 None。"""
    unit, offset = _parse(expr)
    return offset if unit == "day" and offset else None


def is_remote_compatible(expr: str) -> bool:
    """远程用量服务能否表达这个窗口；不能就只在本地记账。"""
    unit, offset = _parse(expr)
    return unit in REMOTE_UNITS or (unit == "day" and offset > 0)


def _month_start(moment: datetime, months: int = 0) -> datetime:
    year, month = moment.year, moment.month + months
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return moment.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def current_window(expr: str, now: datetime | None = None) -> Window:
    """算出 now 落在的窗口。

    `day+11H` 在本地 10:59 时窗口是「昨天 11:00 → 今天 11:00」，标签取窗口**起始日**。
    """
    unit, offset = _parse(expr)
    normalized = normalize(expr)
    local = _as_utc(now or datetime.now(UTC)).astimezone()

    if unit == TOTAL:
        return Window(label=TOTAL, expr=normalized, start=None, end=None)

    if unit == "hour":
        start = local.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        label = start.strftime("%Y-%m-%dT%H")
    elif unit == "week":
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight - timedelta(days=local.weekday())
        end = start + timedelta(weeks=1)
        label = f"{start.isocalendar().year}-W{start.isocalendar().week:02d}"
    elif unit == "month":
        start = _month_start(local)
        end = _month_start(start, months=1)
        label = start.strftime("%Y-%m")
    else:
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight + timedelta(hours=offset)
        if start > local:
            start -= timedelta(days=1)
        end = start + timedelta(days=1)
        label = start.strftime("%Y-%m-%d") + (f"+{offset}H" if offset else "")

    return Window(
        label=label, expr=normalized, start=start.astimezone(UTC), end=end.astimezone(UTC)
    )


def window_label(expr: str, now: datetime | None = None) -> str:
    """usage_counters 的分桶键，与远程的 limitKey 同名。"""
    return current_window(expr, now).label


def window_text(expr: str) -> str:
    """日志用的窗口称呼，不参与任何判定。"""
    unit, offset = _parse(expr)
    if unit == TOTAL:
        return "累计"
    if offset:
        return f"{offset} 点起算的本窗口"
    return {"hour": "本小时", "day": "今日", "week": "本周", "month": "本月"}[unit]
