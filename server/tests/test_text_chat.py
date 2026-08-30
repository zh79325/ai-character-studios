"""文本驱动：请求体、流式与非流式、失败分类。

这一层是唯一真的打网络的地方，所以用 respx 把 HTTP 拦下来。要钉的是协议细节：`stream`
不能被 provider 配置改掉、坏帧不能让半屏回答作废、402 与 429 的处置完全不同。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from atelier.providers import text_chat
from atelier.providers.base import Candidate, ProviderError, QuotaExhausted, RetryableError

URL = "https://example.invalid/v1/chat/completions"


def candidate(**kwargs: object) -> Candidate:
    defaults: dict[str, object] = {
        "provider_model_id": 1,
        "provider_code": "bailian",
        "provider_name": "百炼",
        "model_id": "qwen-plus",
        "driver": "openai_compat",
        "endpoint": "https://example.invalid/v1",
        "api_key": "sk-test",
        "priority": 100,
        "sort_no": 0,
    }
    defaults.update(kwargs)
    return Candidate(**defaults)  # type: ignore[arg-type]


def sse(*frames: str) -> str:
    return "".join(f"data: {f}\n\n" for f in frames)


MESSAGES = [{"role": "user", "content": "你好"}]


# --------------------------------------------------------------------------- #
# 请求体与地址
# --------------------------------------------------------------------------- #


def test_地址只补动作路径() -> None:
    assert text_chat.chat_url(candidate()) == URL
    # 已经写全了就不重复拼
    assert text_chat.chat_url(candidate(endpoint=URL)) == URL


def test_鉴权按provider的风格走() -> None:
    assert text_chat.auth_headers(candidate())["Authorization"] == "Bearer sk-test"
    assert text_chat.auth_headers(candidate(auth_style="x-api-key"))["x-api-key"] == "sk-test"


def test_模型配置改不动平台字段() -> None:
    """一行 extra_body 就能让流式失效或悄悄换掉模型，这里必须锁住。"""
    params = {"extra_body": {"model": "别的模型", "stream": False, "enable_thinking": True}}

    payload = text_chat.build_payload(candidate(params=params), MESSAGES, stream=True)

    assert payload["model"] == "qwen-plus"
    assert payload["stream"] is True
    assert payload["enable_thinking"] is True
    assert payload["stream_options"] == {"include_usage": True}


def test_非流式不加usage开关() -> None:
    payload = text_chat.build_payload(candidate(), MESSAGES, stream=False, temperature=0.3)

    assert "stream_options" not in payload
    assert payload["temperature"] == 0.3


# --------------------------------------------------------------------------- #
# 非流式
# --------------------------------------------------------------------------- #


@respx.mock
def test_非流式拿回内容与真实用量() -> None:
    respx.post(URL).respond(
        json={
            "choices": [{"message": {"content": "好的。"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        },
        headers={"x-ratelimit-remaining-tokens": "8000"},
    )

    reply = text_chat.complete(candidate(), MESSAGES)

    assert reply.content == "好的。"
    assert (reply.prompt_tokens, reply.completion_tokens, reply.total_tokens) == (100, 20, 120)
    assert reply.remaining == 8000
    assert not reply.truncated


@respx.mock
def test_没有usage就不猜() -> None:
    """估算值混进额度台账，后面对不上账时分不清是估歪了还是真用超了。"""
    respx.post(URL).respond(json={"choices": [{"message": {"content": "嗯"}}]})

    reply = text_chat.complete(candidate(), MESSAGES)

    assert reply.total_tokens is None


@respx.mock
def test_被max_tokens截断能看出来() -> None:
    respx.post(URL).respond(
        json={"choices": [{"message": {"content": "说到一半"}, "finish_reason": "length"}]}
    )

    assert text_chat.complete(candidate(), MESSAGES).truncated


@respx.mock
def test_没有choice当失败() -> None:
    respx.post(URL).respond(json={"usage": {}})

    with pytest.raises(ProviderError, match="choice"):
        text_chat.complete(candidate(), MESSAGES)


@respx.mock
def test_返回的不是json当失败() -> None:
    respx.post(URL).respond(text="<html>网关页面</html>")

    with pytest.raises(ProviderError, match="不是 JSON"):
        text_chat.complete(candidate(), MESSAGES)


# --------------------------------------------------------------------------- #
# 流式
# --------------------------------------------------------------------------- #


@respx.mock
def test_流式边收边回调并在done后收尾() -> None:
    respx.post(URL).respond(
        text=sse(
            '{"choices": [{"delta": {"content": "冷光"}}]}',
            '{"choices": [{"delta": {"content": "金属"}, "finish_reason": "stop"}]}',
            '{"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, '
            '"total_tokens": 7}}',
            "[DONE]",
        ),
        headers={"content-type": "text/event-stream"},
    )
    pieces: list[str] = []

    reply = text_chat.complete(candidate(), MESSAGES, on_delta=pieces.append)

    assert pieces == ["冷光", "金属"]
    assert reply.content == "冷光金属"
    assert reply.total_tokens == 7
    assert reply.finish_reason == "stop"


@respx.mock
def test_坏帧跳过而不是整轮作废() -> None:
    """偶发的心跳或被截断的一帧，不该让用户已经看到半屏的回答白费。"""
    respx.post(URL).respond(
        text=(
            ": keep-alive\n\n"
            + sse('{"choices": [{"delta": {"content": "前半"}}]}')
            + "data: {这帧坏了\n\n"
            + sse('{"choices": [{"delta": {"content": "后半"}}]}', "[DONE]")
        ),
        headers={"content-type": "text/event-stream"},
    )

    reply = text_chat.complete(candidate(), MESSAGES, on_delta=lambda _: None)

    assert reply.content == "前半后半"


# --------------------------------------------------------------------------- #
# 失败分类
# --------------------------------------------------------------------------- #


@respx.mock
def test_欠费要换候选() -> None:
    respx.post(URL).respond(status_code=402, text="Arrearage")

    with pytest.raises(QuotaExhausted):
        text_chat.complete(candidate(), MESSAGES)


@respx.mock
def test_纯限流该退避重试() -> None:
    respx.post(URL).respond(status_code=429, text="Requests rate limit exceeded per minute")

    with pytest.raises(RetryableError):
        text_chat.complete(candidate(), MESSAGES)


@respx.mock
def test_额度用尽即使报429也要换候选() -> None:
    respx.post(URL).respond(status_code=429, json={"message": "Free allocated quota exceeded"})

    with pytest.raises(QuotaExhausted):
        text_chat.complete(candidate(), MESSAGES)


@respx.mock
def test_流式也走同一套失败分类() -> None:
    respx.post(URL).respond(status_code=402, text="余额不足")

    with pytest.raises(QuotaExhausted):
        text_chat.complete(candidate(), MESSAGES, on_delta=lambda _: None)


@respx.mock
def test_连不上算可重试() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("连接被拒"))

    with pytest.raises(RetryableError):
        text_chat.complete(candidate(), MESSAGES)


def test_非文本驱动直接拒绝() -> None:
    """配错了 Agent 绑定，早报比把请求发出去再报好。"""
    with pytest.raises(ProviderError, match="不是文本驱动"):
        text_chat.complete(candidate(driver="meshy"), MESSAGES)


# --------------------------------------------------------------------------- #
# 带图的消息
# --------------------------------------------------------------------------- #


def test_带图消息是先文字后图(tmp_path: Path) -> None:
    """先说按什么口径判再递图。反过来的话它已经自己描述完一遍了。"""
    path = tmp_path / "正面.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    message = text_chat.vision_message("看看尾巴分不分离", [path])

    assert message["role"] == "user"
    parts = message["content"]
    assert parts[0] == {"type": "text", "text": "看看尾巴分不分离"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    # 四视图要判细节，不能压成低精度让它猜
    assert parts[1]["image_url"]["detail"] == "high"


def test_多张图按给的顺序排(tmp_path: Path) -> None:
    """整批评审时图的顺序就是视角的顺序，乱了就对不上哪张是背面。"""
    first = tmp_path / "a.png"
    first.write_bytes(b"a")
    second = tmp_path / "b.webp"
    second.write_bytes(b"b")

    parts = text_chat.vision_message("四张一起看", [first, "https://oss.invalid/c.png", second])[
        "content"
    ]

    assert [one["type"] for one in parts] == ["text", "image_url", "image_url", "image_url"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png")
    # 网址原样传，不必把字节拉下来再发一遍
    assert parts[2]["image_url"]["url"] == "https://oss.invalid/c.png"
    assert parts[3]["image_url"]["url"].startswith("data:image/webp")


def test_带图消息能直接进请求体(tmp_path: Path) -> None:
    """分段是协议自己的形态，驱动不再翻译一道。"""
    path = tmp_path / "正面.jpg"
    path.write_bytes(b"jpg")
    message = text_chat.vision_message("审一下", [path])

    payload = text_chat.build_payload(candidate(), [message], stream=False)

    assert payload["messages"][0]["content"][0]["text"] == "审一下"


def test_图不在了当场就报(tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="参考图不存在"):
        text_chat.vision_message("审一下", [tmp_path / "没有这张.png"])
