"""额度窗口：标签算法必须与远程用量服务对得上，否则同一份额度会被记成两条账。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from atelier.providers import period


def _local(year: int, month: int, day: int, hour: int = 0) -> datetime:
    """构造一个本地时刻，再转 UTC——窗口边界按本机时区算。"""
    return datetime(year, month, day, hour, tzinfo=datetime.now().astimezone().tzinfo).astimezone(
        UTC
    )


def test_normalize_collapses_hour_offset_spelling() -> None:
    assert period.normalize("day+09H") == period.normalize("day+9H") == "day+9H"
    assert period.normalize(" day ") == "day"
    assert period.normalize("month") == "month"


@pytest.mark.parametrize("expr", ["", "year", "day+24H", "month+11H", "week+2H", "day+"])
def test_illegal_expr_raises(expr: str) -> None:
    with pytest.raises(period.PeriodExprError):
        period.normalize(expr)


def test_day_label_is_plain_date() -> None:
    assert period.window_label("day", _local(2026, 8, 30, 15)) == "2026-08-30"


def test_month_label_has_no_day() -> None:
    assert period.window_label("month", _local(2026, 8, 30, 15)) == "2026-08"


def test_day_offset_window_starts_at_configured_hour() -> None:
    """day+11H：本地 10:59 还算前一天的窗口，标签取窗口起始日。"""
    assert period.window_label("day+11H", _local(2026, 8, 30, 10)) == "2026-08-29+11H"
    assert period.window_label("day+11H", _local(2026, 8, 30, 11)) == "2026-08-30+11H"


def test_day_offset_window_bounds_span_one_day() -> None:
    window = period.current_window("day+11H", _local(2026, 8, 30, 12))
    assert window.start is not None and window.end is not None
    assert window.end - window.start == timedelta(days=1)
    assert window.contains(_local(2026, 8, 30, 12))
    assert not window.contains(_local(2026, 8, 31, 12))


def test_total_never_resets() -> None:
    """买断式积分池没有窗口，标签恒定，任何时刻都落在窗口内。"""
    window = period.current_window("total")
    assert window.label == "total"
    assert window.start is None and window.end is None
    assert window.contains(_local(2030, 1, 1))
    assert period.window_label("total", _local(2026, 8, 30)) == period.window_label(
        "total", _local(2027, 1, 1)
    )


def test_only_remote_syntax_is_sent_to_usage_server() -> None:
    """远程的 normalize_period 只认 day / month / day+nH，其余只在本地记账。"""
    assert period.is_remote_compatible("day")
    assert period.is_remote_compatible("month")
    assert period.is_remote_compatible("day+11H")
    assert not period.is_remote_compatible("hour")
    assert not period.is_remote_compatible("week")
    assert not period.is_remote_compatible("total")


def test_day_offset_of_reads_start_hour() -> None:
    assert period.day_offset_of("day+11H") == 11
    assert period.day_offset_of("day") is None
    assert period.day_offset_of("month") is None


def test_window_text_is_display_only() -> None:
    assert period.window_text("day") == "今日"
    assert period.window_text("month") == "本月"
    assert period.window_text("total") == "累计"
    assert "11" in period.window_text("day+11H")
