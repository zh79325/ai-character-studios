"""路由契约：候选、决策结果、失败分类、剩余额度解析。

这里只有数据结构与纯函数，不发 HTTP。驱动实现（openai_compat / dashscope_* / ark_* /
meshy）随 A4、A9 落，届时只需产出 `CallOutcome` 交给 router 记账。

额度剩余以供应商响应头为准，本地不做估算；头里读不到就当无限额，撞到额度错误再切下家。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

# 计量口径：文本记 tokens，生图/视频记 calls，Meshy 记 credits
LIMIT_KINDS = ("tokens", "calls", "credits")

# OpenAI 兼容端点的事实标准头。方舟、百炼 compatible-mode 沿用这套命名；
# 读不到就当无限额，不猜别的名字。Meshy 余额不在响应头里，只能靠调用报错发现。
REMAINING_HEADERS: dict[str, tuple[str, ...]] = {
    "tokens": ("x-ratelimit-remaining-tokens",),
    "calls": ("x-ratelimit-remaining-requests",),
    "credits": (),
}

# 错误体里出现这些词，才判定是额度用尽（要换候选）而不是限流（该退避重试）
_QUOTA_MARKERS = re.compile(
    r"quota|insufficient|exceeded|exhaust|arrearage|balance|欠费|余额|额度",
    re.IGNORECASE,
)

# 限流的措辞先认：百炼与方舟的 QPM/TPM 报错里就带着 "exceeded"，只看额度词会把「等一秒就好」
# 误判成「这个账号没额度了」，白白把候选标满并换走。
_RATE_LIMIT_MARKERS = re.compile(
    r"rate.?limit|too many requests|per (?:minute|second)|qpm|tpm|rpm|频繁|限流",
    re.IGNORECASE,
)


class ProviderError(RuntimeError):
    """provider 调用失败的基类。"""


class RetryableError(ProviderError):
    """限流或临时故障，退避重试可能成功；重试用尽才换候选。"""


class QuotaExhausted(ProviderError):
    """该候选本窗口额度已尽，重试无意义，直接换下一个。"""


class NoCandidateError(ProviderError):
    """该 Agent 没有可用候选：没配、全禁用、全熔断或全部额度用尽。"""


@runtime_checkable
class StickyBinding(Protocol):
    """会话粘性绑定的读写口，选路层通过它记住「这个会话在用哪个候选」。

    会话行住在**项目自己的库**里（项目目录可以在磁盘任意位置），而候选来自全局库，两边
    不能 join、也不共用 Session。所以选路层不去查会话，改由调用方把会话对象传进来，选路
    层只按这个协议读写字段，最后由调用方提交自己那个库。`atelier.db.project_models`
    的 `Conversation` 天然满足它。
    """

    id: str
    bound_provider_model_id: int | None
    bound_provider_label: str
    bound_at: datetime | None
    rebind_count: int
    rebind_reason: str | None


@dataclass(frozen=True, slots=True)
class Candidate:
    """一个可调用的 provider × model，按 priority 升序排队。

    priority 取自 provider，同 priority 内按 provider_code + sort_no 稳定排序，
    保证选路结果可复现——顺序由配置的人决定，路由层不做打分、不做均衡。
    """

    provider_model_id: int
    provider_code: str
    provider_name: str
    model_id: str
    driver: str
    endpoint: str
    api_key: str
    priority: int
    sort_no: int
    verify_ssl: bool = True
    auth_style: str = "bearer"
    params: dict[str, object] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """日志用的可读标识，不含 api_key。"""
        return f"{self.provider_code}/{self.model_id}"

    def sort_key(self) -> tuple[int, int, str, str]:
        return (self.priority, self.sort_no, self.provider_code, self.model_id)


@dataclass(frozen=True, slots=True)
class Decision:
    """一次选路结果。outcome 与 route_logs.outcome 同义。"""

    candidate: Candidate
    outcome: str
    reason: str | None = None
    conversation_id: str | None = None
    skipped: tuple[tuple[str, str], ...] = ()
    """被跳过的候选与原因，(label, reason)，写进 route_logs 便于复盘。"""


@dataclass(frozen=True, slots=True)
class CallOutcome:
    """调用完成后交回路由层记账的事实数据。

    used_delta 为本次实际消耗（tokens / calls / credits），remaining 为供应商报告的
    剩余额度；两者都可能拿不到，拿不到就不写。
    """

    limit_kind: str
    used_delta: int | None = None
    remaining: int | None = None
    latency_ms: int | None = None
    reported_at: datetime | None = None


def auth_headers(candidate: Candidate) -> dict[str, str]:
    """鉴权头。风格配在 provider 上，所有驱动共用这一份判断。

    各驱动各写一遍的话，新加一种鉴权风格时必然只改到其中一个，另一个继续按老规矩发。
    """
    if candidate.auth_style == "x-api-key":
        return {"x-api-key": candidate.api_key}
    return {"Authorization": f"Bearer {candidate.api_key}"}


def parse_remaining(headers: Mapping[str, str], limit_kind: str) -> int | None:
    """从响应头读供应商报告的剩余额度，读不到返回 None（视为无限额）。"""
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in REMAINING_HEADERS.get(limit_kind, ()):
        raw = lowered.get(name)
        if raw is None:
            continue
        try:
            return int(float(raw.strip()))
        except ValueError:
            continue
    return None


def classify_failure(status_code: int | None, body: str = "") -> ProviderError:
    """把 HTTP 失败分成「换候选」与「退避重试」两类。

    402 一律是欠费或额度用尽；429 先按限流看，只有措辞明确指向额度且不带限流字样才算耗尽；
    其余 4xx 看额度字样。5xx、超时、连接失败按可重试处理。
    """
    if status_code == 402:
        return QuotaExhausted(f"HTTP 402 欠费或额度用尽：{body[:200]}")
    if status_code in (401, 403):
        return ProviderError(f"HTTP {status_code} 凭证无效或无权限：{body[:200]}")
    if status_code == 429:
        if _QUOTA_MARKERS.search(body) and not _RATE_LIMIT_MARKERS.search(body):
            return QuotaExhausted(f"HTTP 429 额度用尽：{body[:200]}")
        return RetryableError(f"HTTP 429 限流：{body[:200]}")
    if status_code is not None and 400 <= status_code < 500:
        if _QUOTA_MARKERS.search(body):
            return QuotaExhausted(f"HTTP {status_code} 额度用尽：{body[:200]}")
        return ProviderError(f"HTTP {status_code}：{body[:200]}")
    return RetryableError(f"HTTP {status_code or '连接失败'}：{body[:200]}")
