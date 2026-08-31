"""文本对话驱动：OpenAI 兼容的 `/chat/completions`。

只做一件事：把消息发出去、把结果与真实用量带回来。选路、记账、熔断都不在这里——那是
`providers/router` 的职责，本模块拿到失败只负责翻译成 `classify_failure` 那套分类，
由调用方决定换候选还是退避重试。

为什么只有 OpenAI 兼容一种驱动：百炼 Token Plan 的 `/compatible-mode/v1` 与方舟的
`/api/v3` 都是这套协议，文本这一路没必要各写一个。真需要私有协议的（Meshy、生图的
dashscope_mm）不走文本，随 A9 落各自的驱动。

流式与非流式共用同一条路径：给了 `on_delta` 就开 `stream=True` 边收边回调，最后仍然
返回同一个 `ChatReply`。这样上层（会话引擎）不必写两套记账逻辑。

消息的 `content` 既可以是一句话，也可以是「文字 + 图」的分段列表——看图评审（四视图的
`vision_reviewer`）走的就是后者。分段是 OpenAI 兼容协议自己的形态，两家都认，所以本模块只
把类型放宽到 `Any` 并提供一个拼分段的 `vision_message`，不另立一套私有结构：私有结构迟早
要在驱动里再翻译回来，等于同一件事写两遍。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
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
from atelier.providers.image_gen import reference_ref
from atelier.settings import get_settings

__all__ = [
    "CHAT_PATH",
    "TEXT_DRIVERS",
    "ChatReply",
    "auth_headers",
    "build_payload",
    "chat_url",
    "complete",
    "output_budget",
    "vision_message",
]

_log = structlog.get_logger(__name__)

TEXT_DRIVERS = ("openai_compat",)
"""能走本模块的 driver。别的 driver 说明配错了 Agent 绑定，早报比发出去再报好。"""

CHAT_PATH = "chat/completions"

DEFAULT_TIMEOUT = 120.0
"""长回答（art-bible 六节全文）几十秒是常态，超时给足；连接阶段单独卡短。"""

CONNECT_TIMEOUT = 10.0

IMAGE_DETAIL = "high"
"""看图精度。四视图评审要判「双尾是不是分离」这种细节，压成低精度等于让它猜。"""

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
    reasoning: str = ""
    """推理模型的思考过程（`reasoning_content`）。

    收下但不并进 `content`：它不是给用户看的正文，混进去会污染定稿。留着是为了解释空回答——
    推理几千字、正文一个字都没有，说明输出预算全烧在思考上了，这跟被安全策略拦掉是两回事。
    """

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
    messages: Sequence[Mapping[str, Any]],
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


def vision_message(
    text: str,
    images: Sequence[str | Path],
    *,
    role: str = "user",
    detail: str = IMAGE_DETAIL,
) -> dict[str, Any]:
    """拼一条带图的消息：先文字后图。

    文字在前是有意的——要先告诉它看什么、按什么口径判，再把图递过去。反过来的话它已经
    自己描述完一遍了，后面的要求往往只能拿来套自己的描述。

    图得连字节一起发（本地文件转 data URL），这事 `image_gen.reference_ref` 已经在做，直接
    用它：同一份 MIME 白名单写两遍，下一次添格式时必然只改到一边。
    """
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    parts.extend(
        {"type": "image_url", "image_url": {"url": reference_ref(one), "detail": detail}}
        for one in images
    )
    return {"role": role, "content": parts}


def output_budget(candidate: Candidate, explicit: int | None) -> int:
    """这一轮最多让模型说多少 token。

    从来不传 `max_tokens` 的坏处不是花钱，是各家默认值差得离谱（有的几百 token 就截断），
    同一段提示词换个模型就答一半。显式参数优先，其次模型自己配的，最后全局兜底。
    """
    if explicit is not None:
        return explicit
    configured = candidate.params.get("max_output_tokens")
    if isinstance(configured, int | float) and configured > 0:
        return int(configured)
    return get_settings().default_max_output_tokens


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
    messages: Sequence[Mapping[str, Any]],
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
        candidate,
        messages,
        stream=stream,
        temperature=temperature,
        max_tokens=output_budget(candidate, max_tokens),
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
        reasoning=reply.reasoning,
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
        reasoning=str(message.get("reasoning_content") or ""),
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
    thinking: list[str] = []
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
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    chunks.append(piece)
                    on_delta(piece)
                # 思考片段不进 `on_delta`：前端那条流是直接往消息里拼的，混进去就成了定稿正文
                thought = delta.get("reasoning_content")
                if thought:
                    thinking.append(thought)
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

    return ChatReply(
        content="".join(chunks),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        remaining=remaining,
        finish_reason=finish_reason,
        reasoning="".join(thinking),
    )
