"""文本对话驱动：OpenAI 兼容的 `/chat/completions`。

只做一件事：把消息发出去、把结果与真实用量带回来。选路、记账、熔断都不在这里——那是
`providers/router` 的职责，本模块拿到失败只负责翻译成 `classify_failure` 那套分类，
由调用方决定换候选还是退避重试。

为什么只有 OpenAI 兼容一种驱动：百炼 Token Plan 的 `/compatible-mode/v1` 与方舟的
`/api/v3` 都是这套协议，文本这一路没必要各写一个。真需要私有协议的（Meshy、生图的
dashscope_mm）不走文本，随 A9 落各自的驱动。

流式与非流式共用同一条路径：给了 `on_delta` 就开 `stream=True` 边收边回调，最后仍然
返回同一个 `ChatReply`。这样上层（会话引擎）不必写两套记账逻辑。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from atelier.providers.base import (
    Candidate,
    ProviderError,
    RetryableError,
    auth_headers,
    classify_failure,
    parse_remaining,
)

__all__ = [
    "CHAT_PATH",
    "TEXT_DRIVERS",
    "ChatReply",
    "auth_headers",
    "build_payload",
    "chat_url",
    "complete",
]

_log = structlog.get_logger(__name__)

TEXT_DRIVERS = ("openai_compat",)
"""能走本模块的 driver。别的 driver 说明配错了 Agent 绑定，早报比发出去再报好。"""

CHAT_PATH = "chat/completions"

DEFAULT_TIMEOUT = 120.0
"""长回答（art-bible 六节全文）几十秒是常态，超时给足；连接阶段单独卡短。"""

CONNECT_TIMEOUT = 10.0

_DONE = "[DONE]"


@dataclass(frozen=True, slots=True)
class ChatReply:
    """一次文本调用的结果与用量事实。

    三个 token 字段都可能是 None：供应商不返回 usage 时不猜——估算值写进 route_logs 会
    让后面对额度的复盘失真。
    """

    content: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    remaining: int | None = None
    latency_ms: int = 0
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        """回答是被 max_tokens 截断的，而不是自然说完。"""
        return self.finish_reason == "length"


def chat_url(candidate: Candidate) -> str:
    """拼调用地址。endpoint 已经指到 `/v1` 这一级，这里只补动作路径。"""
    base = candidate.endpoint.rstrip("/")
    return base if base.endswith(CHAT_PATH) else f"{base}/{CHAT_PATH}"


def build_payload(
    candidate: Candidate,
    messages: Sequence[Mapping[str, str]],
    *,
    stream: bool,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """请求体。模型上配的 `params.extra_body` 原样合并进去。

    合并顺序让平台字段在后：`model` / `messages` / `stream` 是本模块的语义，不允许被
    provider 配置改写，否则一行配置就能让流式失效或换掉模型。
    """
    extra = candidate.params.get("extra_body")
    payload: dict[str, Any] = dict(extra) if isinstance(extra, dict) else {}

    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    payload["model"] = candidate.model_id
    payload["messages"] = [dict(m) for m in messages]
    payload["stream"] = stream
    if stream:
        # 流式默认不带 usage，加上它才能拿到真实 token 数记账（百炼、方舟都支持）
        payload["stream_options"] = {"include_usage": True}
    return payload


def _usage_of(data: Mapping[str, Any]) -> tuple[int | None, int | None, int | None]:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None, None, None

    def read(key: str) -> int | None:
        value = usage.get(key)
        return int(value) if isinstance(value, int | float) else None

    return read("prompt_tokens"), read("completion_tokens"), read("total_tokens")


def complete(
    candidate: Candidate,
    messages: Sequence[Mapping[str, str]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    on_delta: Callable[[str], None] | None = None,
) -> ChatReply:
    """发一次对话请求。给了 `on_delta` 就走流式，每收到一段就回调一次。"""
    if candidate.driver not in TEXT_DRIVERS:
        raise ProviderError(
            f"{candidate.label} 的 driver={candidate.driver} 不是文本驱动，"
            f"文本 Agent 只能绑 {TEXT_DRIVERS} 之一"
        )

    stream = on_delta is not None
    payload = build_payload(
        candidate, messages, stream=stream, temperature=temperature, max_tokens=max_tokens
    )
    started = time.perf_counter()
    limits = httpx.Timeout(timeout, connect=CONNECT_TIMEOUT)

    try:
        with httpx.Client(verify=candidate.verify_ssl, timeout=limits) as client:
            if on_delta is not None:
                reply = _read_stream(client, candidate, payload, on_delta)
            else:
                reply = _read_once(client, candidate, payload)
    except httpx.HTTPError as exc:
        raise RetryableError(f"{candidate.label} 连接失败：{exc}") from exc

    elapsed = int((time.perf_counter() - started) * 1000)
    _log.info(
        "chat",
        candidate=candidate.label,
        stream=stream,
        latency_ms=elapsed,
        total_tokens=reply.total_tokens,
        finish_reason=reply.finish_reason,
    )
    return ChatReply(
        content=reply.content,
        prompt_tokens=reply.prompt_tokens,
        completion_tokens=reply.completion_tokens,
        total_tokens=reply.total_tokens,
        remaining=reply.remaining,
        latency_ms=elapsed,
        finish_reason=reply.finish_reason,
    )


def _read_once(client: httpx.Client, candidate: Candidate, payload: Mapping[str, Any]) -> ChatReply:
    response = client.post(chat_url(candidate), headers=auth_headers(candidate), json=payload)
    if response.status_code >= 400:
        raise classify_failure(response.status_code, response.text)

    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError(f"{candidate.label} 返回的不是 JSON：{response.text[:200]}") from exc

    choices = data.get("choices") or []
    if not choices:
        raise ProviderError(f"{candidate.label} 没有返回任何 choice：{response.text[:200]}")
    message = choices[0].get("message") or {}
    prompt_tokens, completion_tokens, total_tokens = _usage_of(data)

    return ChatReply(
        content=str(message.get("content") or ""),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        remaining=parse_remaining(response.headers, "tokens"),
        finish_reason=choices[0].get("finish_reason"),
    )


def _read_stream(
    client: httpx.Client,
    candidate: Candidate,
    payload: Mapping[str, Any],
    on_delta: Callable[[str], None],
) -> ChatReply:
    """收 SSE 流。

    坏帧跳过而不是整轮失败：偶发的心跳、注释行、被截断的一帧不该让用户已经看到半屏的
    回答作废。真正的失败在 HTTP 状态码上，那条路径照常抛。
    """
    chunks: list[str] = []
    finish_reason: str | None = None
    prompt_tokens = completion_tokens = total_tokens = None

    with client.stream(
        "POST", chat_url(candidate), headers=auth_headers(candidate), json=payload
    ) as response:
        if response.status_code >= 400:
            response.read()
            raise classify_failure(response.status_code, response.text)

        remaining = parse_remaining(response.headers, "tokens")
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            raw = line[len("data:") :].strip()
            if not raw or raw == _DONE:
                continue
            try:
                frame = json.loads(raw)
            except ValueError:
                _log.warning("chat_stream_bad_frame", candidate=candidate.label, frame=raw[:120])
                continue

            got = _usage_of(frame)
            if got != (None, None, None):
                prompt_tokens, completion_tokens, total_tokens = got

            for choice in frame.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    chunks.append(piece)
                    on_delta(piece)
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

    return ChatReply(
        content="".join(chunks),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        remaining=remaining,
        finish_reason=finish_reason,
    )
