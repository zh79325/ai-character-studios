"""远程用量服务客户端（api-useage-server 对接层）。

同一批 api_key 会被多个工具、多台机器同时用，本地记账各记一套就会把额度合起来打穿。
额度向服务端申请：判定与扣减在服务端同一个事务里完成，并发也不超发。

service 命名沿用 `llm_model_tokens:{provider}:{model}`，keyId 用 md5(api_key)——
只要 provider_code 与其他工具配得一致，同一批 key 的额度就是合并统计的。

真实 key 不出本机：只发 md5 与掩码。远程任何异常都返回 None 交给上层回退本地镜像，
并在 retry_interval 内不再重试，避免每次调用白等一个超时。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from atelier.providers import period as period_mod
from atelier.settings import get_settings

SERVICE_PREFIX = "llm_model_tokens"

_log = structlog.get_logger(__name__)


def service_of(provider_code: str, model_id: str) -> str:
    """一个 (provider, model) 就是一条账。"""
    return f"{SERVICE_PREFIX}:{provider_code}:{model_id}"


def key_id_of(api_key: str) -> str:
    """记账身份：免费额度是账户级的，用 key 的 md5 做标识，真实 key 不外发。"""
    return hashlib.md5((api_key or "").encode("utf-8")).hexdigest()


def mask_key(api_key: str) -> str:
    """展示用掩码，前 4 + 后 4。"""
    key = (api_key or "").strip()
    if not key:
        return "***"
    if len(key) <= 8:
        return f"{key[:2]}***"
    return f"{key[:4]}***{key[-4:]}"


@dataclass(frozen=True, slots=True)
class Permit:
    """一次额度许可。granted=False 即该窗口额度用尽，换下一个候选。"""

    granted: bool
    used: int
    limit: int
    remaining: int | None = None


class UsageClient:
    """线程安全的用量服务客户端，失败自我熔断。"""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        retry_interval: float | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url if base_url is not None else settings.usage_server_url).rstrip(
            "/"
        )
        self._timeout = timeout if timeout is not None else settings.usage_server_timeout
        self._retry_interval = (
            retry_interval if retry_interval is not None else settings.usage_server_retry_interval
        )
        self._lock = threading.Lock()
        self._disabled_until = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    def enabled(self) -> bool:
        """配了地址且不在熔断窗口内。"""
        if not self._base_url:
            return False
        with self._lock:
            return time.monotonic() >= self._disabled_until

    def supports(self, period: str) -> bool:
        """远程能不能记这个窗口的账。

        远程的 normalize_period 只认 day / month / day+nH，hour、week、total 发过去只会招来
        400，白等一个往返；这类窗口直接只在本地记账。
        """
        try:
            return period_mod.is_remote_compatible(period)
        except period_mod.PeriodExprError:
            return False

    def self_disable(self, reason: str) -> None:
        """熔断：接下来一段时间只走本地镜像。"""
        with self._lock:
            already = time.monotonic() < self._disabled_until
            self._disabled_until = time.monotonic() + max(self._retry_interval, 1.0)
        if not already:
            _log.warning(
                "用量服务不可用，改用本地镜像",
                reason=reason,
                fallback_seconds=int(self._retry_interval),
            )

    def reset(self) -> None:
        """清掉熔断状态（改配置后或测试里用）。"""
        with self._lock:
            self._disabled_until = 0.0

    def acquire(
        self,
        service: str,
        api_key: str,
        period: str,
        max_value: int,
        delta: int = 1,
        exhausted: bool = False,
    ) -> Permit | None:
        """申请 delta 的额度许可；远程不可用返回 None 交给上层回退。

        exhausted=True 用于接口实报额度用尽时把远程计数拉到上限，让其他机器也跳过。
        """
        payload = self._key_payload(service, api_key, period, max_value)
        payload["delta"] = int(delta)
        if exhausted:
            payload["exhausted"] = True

        data = self._post("/api/usage/acquire", payload)
        if not isinstance(data, dict) or "granted" not in data or "quta" not in data:
            return None
        try:
            return Permit(
                granted=bool(data["granted"]),
                used=int(data["quta"]),
                limit=int(data.get("limit") or 0),
                remaining=_optional_int(data.get("remaining")),
            )
        except (TypeError, ValueError):
            return None

    def snapshot(self, service: str, period: str) -> list[dict[str, Any]] | None:
        """取该 service 当前窗口全部 key 的用量条目，远程不可用返回 None。

        条目形如 {'keyId', 'keyMask', 'limitKey', 'limit', 'quta'}。
        """
        data = self._post(
            "/api/usage/snapshot", {"service": service, "period": period_mod.normalize(period)}
        )
        if not isinstance(data, dict):
            return None
        items = data.get("items")
        return items if isinstance(items, list) else None

    def _key_payload(
        self, service: str, api_key: str, period: str, max_value: int
    ) -> dict[str, Any]:
        return {
            "service": service,
            "keyId": key_id_of(api_key),
            "keyMask": mask_key(api_key),
            "period": period_mod.normalize(period),
            "maxCalls": int(max_value or 0),
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.enabled():
            return None
        if not self.supports(str(payload.get("period") or "")):
            return None
        try:
            response = httpx.post(
                f"{self._base_url}{path}",
                json=payload,
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if 400 <= code < 500 and code not in (401, 403, 404, 408, 429):
                # 参数问题，重试也一样：不熔断，本次回退本地
                _log.warning("用量服务拒绝请求，本次改用本地镜像", status=code, path=path)
                return None
            self.self_disable(f"HTTP {code}")
            return None
        except (httpx.HTTPError, ValueError) as exc:
            self.self_disable(f"{type(exc).__name__}: {exc}")
            return None

        if not isinstance(data, dict):
            self.self_disable("返回体不是 JSON 对象")
            return None
        return data


def _optional_int(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


_client: UsageClient | None = None
_client_lock = threading.Lock()


def get_usage_client() -> UsageClient:
    """进程内共用一个客户端，熔断状态才能生效。"""
    global _client
    with _client_lock:
        if _client is None:
            _client = UsageClient()
        return _client


def reset_usage_client() -> None:
    """丢弃单例，下次重新读配置（改设置后或测试里用）。"""
    global _client
    with _client_lock:
        _client = None
