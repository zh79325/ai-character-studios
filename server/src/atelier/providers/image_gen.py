"""生图驱动：方舟 `images/generations` 与百炼多模态生成。

这两家的协议差得远，不像文本那样能共用一套 OpenAI 兼容：方舟是 `{"prompt": ..., "size":
"2048x2048"}` 平铺一层、返回 `data[].url`；百炼把提示词塞进 `input.messages[].content`、
出参在 `output.choices[].message.content[].image`。共用的只有「拿回来的是一张图的字节」这
个结果，所以本模块对上只暴露 `generate()` 和 `ImageReply`，驱动差异全压在里面。

**为什么一定要把图下载成字节**：两家给的都是几小时后过期的签名地址。把地址存进 meta.json
等于存了一条明天就打不开的链接，而素材库的价值恰恰在于半年后还能翻出来看。

**为什么尺寸要现场量**：`size` 是请求意图，不是事实——供应商会按自己支持的档位取整。四视
图那一步要靠真实尺寸判断是不是同一规格，拿请求值去判断会把已经走形的图放过去。

记账口径是 `calls`：生图按张计费（方舟 Agent Plan 一张 99 AFP），调用前就知道消耗，与文本
的「事后读 usage」是两回事，由 `agents/dispatch` 按 `limit_kind="calls"` 预扣。
"""

from __future__ import annotations

import base64
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import structlog
from PIL import Image, UnidentifiedImageError

from atelier.providers.base import (
    Candidate,
    ProviderError,
    RetryableError,
    auth_headers,
    classify_failure,
    parse_remaining,
)

_log = structlog.get_logger(__name__)

ARK = "ark_image"
DASHSCOPE = "dashscope_mm"

IMAGE_DRIVERS = (ARK, DASHSCOPE)
"""能走本模块的 driver。别的 driver 说明 Agent 绑错了模型，早报比发出去再报好。"""

DEFAULT_SIZE = 2048
"""缺省边长。项目 `defaults.image_size` 没配时用它——2K 够看清材质，又不至于每张都顶到
4K 的价钱。"""

DEFAULT_TIMEOUT = 300.0
"""4K 出图三五分钟是常态，超时给足；连接阶段单独卡短。"""

CONNECT_TIMEOUT = 10.0
DOWNLOAD_TIMEOUT = 120.0

MAX_BYTES = 40 * 1024 * 1024
"""单张上限。超过这个数基本是拿错了地址（拿到压缩包或视频），不往素材库里塞。"""

SUFFIXES = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
"""认得的格式。素材库只收这三种：其余格式后续的 i2i、切图工具链都不认。"""

MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

_TIERS = ((2048, "2K"), (3072, "3K"), (4096, "4K"))


@dataclass(frozen=True, slots=True)
class ImageReply:
    """一次生图调用的结果与参数事实。

    `params` 是生效后的完整参数快照，直接落进 meta.json：半年后想复现这张图，靠的就是它，
    所以宁可多记也不省。里面不含 api_key，参考图也只留字节数不留内容。
    """

    data: bytes
    suffix: str
    width: int
    height: int
    params: dict[str, Any] = field(default_factory=dict)
    remaining: int | None = None
    latency_ms: int = 0

    @property
    def size_text(self) -> str:
        return f"{self.width}x{self.height}"


def negative_field(candidate: Candidate) -> str | None:
    """这个模型把 negative_prompt 放在哪个字段上，不支持就返回 None。

    百炼的多模态生成有这个参数，方舟的 seedream 没有。不支持时**不把它拼进 prompt** 凑数：
    正向提示词里出现「不要有背景」这类话，扩散模型往往反而把背景画出来，而且事后没人分得清
    那句话是设定卡片写的还是平台塞的。真实情况写进快照，让 vision_reviewer 自己判断。
    """
    configured = candidate.params.get("negative_prompt_field")
    if isinstance(configured, str):
        return configured or None
    return "negative_prompt" if candidate.driver == DASHSCOPE else None


def ark_size(candidate: Candidate, width: int, height: int) -> str:
    """方舟的 size 取值。默认给精确像素，模型只认档位时在 params 里配 `size_style=tier`。

    档位会把非正方形的比例抹掉，所以不作为默认。
    """
    if candidate.params.get("size_style") != "tier":
        return f"{width}x{height}"
    longest = max(width, height)
    for limit, name in _TIERS:
        if longest <= limit:
            return name
    return _TIERS[-1][1]


def reference_ref(item: str | Path) -> str:
    """参考图交给供应商的形态：网址原样传，本地文件转成 data URL。

    本地文件不可能让对方来取，只能连字节一起发过去。
    """
    if isinstance(item, str) and item.startswith(("http://", "https://", "data:")):
        return item
    path = Path(item)
    if not path.is_file():
        raise ProviderError(f"参考图不存在：{path}")
    mime = MIME_TYPES.get(path.suffix.lower())
    if mime is None:
        raise ProviderError(f"参考图格式不支持：{path.name}，只收 {sorted(MIME_TYPES)}")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def build_payload(
    candidate: Candidate,
    prompt: str,
    *,
    negative_prompt: str = "",
    width: int = DEFAULT_SIZE,
    height: int = DEFAULT_SIZE,
    seed: int | None = None,
    references: Sequence[str] = (),
) -> dict[str, Any]:
    """按 driver 拼请求体。模型上配的 `params.extra_body` 作为底，平台字段在后覆盖。

    顺序让平台字段在后：`model` / `prompt` / `size` 是本模块的语义，一行 provider 配置就
    能把模型换掉或把尺寸改掉的话，出来的图和 meta.json 记的就不是一回事了。
    """
    if candidate.driver == ARK:
        return _ark_payload(
            candidate,
            prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            seed=seed,
            references=references,
        )
    return _dashscope_payload(
        candidate,
        prompt,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        seed=seed,
        references=references,
    )


def _extra_body(candidate: Candidate) -> dict[str, Any]:
    extra = candidate.params.get("extra_body")
    return dict(extra) if isinstance(extra, dict) else {}


def _ark_payload(
    candidate: Candidate,
    prompt: str,
    *,
    negative_prompt: str,
    width: int,
    height: int,
    seed: int | None,
    references: Sequence[str],
) -> dict[str, Any]:
    payload = _extra_body(candidate)
    payload.setdefault("watermark", False)
    if seed is not None:
        payload["seed"] = seed
    field_name = negative_field(candidate)
    if negative_prompt and field_name:
        payload[field_name] = negative_prompt

    payload["model"] = candidate.model_id
    payload["prompt"] = prompt
    payload["size"] = ark_size(candidate, width, height)
    # 固定要地址：本模块要把字节拿到手，b64 也能收但没必要让对方多编码一遍
    payload["response_format"] = "url"
    if references:
        payload["image"] = list(references)
    return payload


def _dashscope_payload(
    candidate: Candidate,
    prompt: str,
    *,
    negative_prompt: str,
    width: int,
    height: int,
    seed: int | None,
    references: Sequence[str],
) -> dict[str, Any]:
    parameters = _extra_body(candidate)
    parameters.setdefault("n", 1)
    parameters.setdefault("watermark", False)
    if seed is not None:
        parameters["seed"] = seed
    field_name = negative_field(candidate)
    if negative_prompt and field_name:
        parameters[field_name] = negative_prompt
    # 百炼用 * 分隔，写成 x 会被判成非法尺寸
    parameters["size"] = f"{width}*{height}"

    content: list[dict[str, str]] = [{"image": one} for one in references]
    content.append({"text": prompt})
    return {
        "model": candidate.model_id,
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": parameters,
    }


def generate(
    candidate: Candidate,
    prompt: str,
    *,
    negative_prompt: str = "",
    width: int = DEFAULT_SIZE,
    height: int = DEFAULT_SIZE,
    seed: int | None = None,
    references: Sequence[str | Path] = (),
    timeout: float = DEFAULT_TIMEOUT,
) -> ImageReply:
    """出一张图，连字节一起带回来。

    失败只翻译成 `classify_failure` 那套分类，换候选还是退避重试由 `dispatch` 决定。
    """
    if candidate.driver not in IMAGE_DRIVERS:
        raise ProviderError(
            f"{candidate.label} 的 driver={candidate.driver} 不是生图驱动，"
            f"生图 Agent 只能绑 {IMAGE_DRIVERS} 之一"
        )
    if not prompt.strip():
        raise ProviderError(f"{candidate.label} 的 prompt 是空的，这一次不发出去")

    refs = tuple(reference_ref(one) for one in references)
    payload = build_payload(
        candidate,
        prompt,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        seed=seed,
        references=refs,
    )
    started = time.perf_counter()
    limits = httpx.Timeout(timeout, connect=CONNECT_TIMEOUT)

    try:
        with httpx.Client(verify=candidate.verify_ssl, timeout=limits) as client:
            response = client.post(
                candidate.endpoint, headers=auth_headers(candidate), json=payload
            )
            if response.status_code >= 400:
                raise classify_failure(response.status_code, response.text)
            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError(
                    f"{candidate.label} 返回的不是 JSON：{response.text[:200]}"
                ) from exc

            _raise_inline_error(candidate, data)
            picked = _pick(candidate, data)
            remaining = parse_remaining(response.headers, "calls")
            raw = picked if isinstance(picked, bytes) else _download(client, candidate, picked)
    except httpx.HTTPError as exc:
        raise RetryableError(f"{candidate.label} 连接失败：{exc}") from exc

    elapsed = int((time.perf_counter() - started) * 1000)
    actual_width, actual_height, suffix = _describe(candidate, raw)
    _log.info(
        "image_gen",
        candidate=candidate.label,
        latency_ms=elapsed,
        size=f"{actual_width}x{actual_height}",
        bytes=len(raw),
    )
    return ImageReply(
        data=raw,
        suffix=suffix,
        width=actual_width,
        height=actual_height,
        params=snapshot(
            candidate,
            payload,
            negative_prompt=negative_prompt,
            requested=(width, height),
            actual=(actual_width, actual_height),
            references=len(refs),
            latency_ms=elapsed,
        ),
        remaining=remaining,
        latency_ms=elapsed,
    )


def _raise_inline_error(candidate: Candidate, data: Mapping[str, Any]) -> None:
    """百炼会用 HTTP 200 带 `code` 报错。当成失败处理，否则下一步只会看到「没有返回图片」。"""
    code = data.get("code")
    if isinstance(code, str) and code:
        message = str(data.get("message") or "")
        raise classify_failure(400, f"{candidate.label} {code}：{message}")


def _pick(candidate: Candidate, data: Mapping[str, Any]) -> str | bytes:
    """从响应里取出这张图：拿到地址返回 str，拿到内联 base64 直接返回字节。"""
    if candidate.driver == ARK:
        for item in data.get("data") or []:
            if not isinstance(item, dict):
                continue
            inline = item.get("b64_json")
            if isinstance(inline, str) and inline:
                return _decode(candidate, inline)
            url = item.get("url")
            if isinstance(url, str) and url:
                return url
    else:
        output = data.get("output")
        choices = output.get("choices") or [] if isinstance(output, dict) else []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            parts = message.get("content") or [] if isinstance(message, dict) else []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                url = part.get("image")
                if isinstance(url, str) and url:
                    return _decode(candidate, url) if url.startswith("data:") else url

    raise ProviderError(f"{candidate.label} 没有返回图片：{str(data)[:200]}")


def _decode(candidate: Candidate, blob: str) -> bytes:
    payload = blob.split(",", 1)[1] if blob.startswith("data:") else blob
    try:
        # binascii.Error 是 ValueError 的子类，这一条就够
        return base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise ProviderError(f"{candidate.label} 返回的 base64 解不开") from exc


def _download(client: httpx.Client, candidate: Candidate, url: str) -> bytes:
    """把签名地址上的图取下来。

    取不到按可重试处理：签名地址过期、对象存储抖动都属于换个候选重出一张就能绕开的问题，
    而不是提示词有毛病。
    """
    response = client.get(url, timeout=httpx.Timeout(DOWNLOAD_TIMEOUT, connect=CONNECT_TIMEOUT))
    if response.status_code >= 400:
        raise RetryableError(
            f"{candidate.label} 的图片没取下来（HTTP {response.status_code}）："
            "签名地址通常只活几小时"
        )
    raw = response.content
    if not raw:
        raise ProviderError(f"{candidate.label} 的图片地址返回了 0 字节")
    if len(raw) > MAX_BYTES:
        raise ProviderError(f"{candidate.label} 返回了 {len(raw)} 字节，超过单张上限 {MAX_BYTES}")
    return raw


def _describe(candidate: Candidate, raw: bytes) -> tuple[int, int, str]:
    """现场量真实尺寸与格式。识别不出来就当失败——存进素材库的必须是能打开的图。"""
    try:
        with Image.open(BytesIO(raw)) as image:
            kind = (image.format or "").upper()
            width, height = image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise ProviderError(f"{candidate.label} 返回的 {len(raw)} 字节不是能识别的图片") from exc

    suffix = SUFFIXES.get(kind)
    if suffix is None:
        raise ProviderError(
            f"{candidate.label} 返回了 {kind or '未知'} 格式，素材库只收 {sorted(SUFFIXES)}"
        )
    return width, height, suffix


def snapshot(
    candidate: Candidate,
    payload: Mapping[str, Any],
    *,
    negative_prompt: str,
    requested: tuple[int, int],
    actual: tuple[int, int],
    references: int,
    latency_ms: int,
) -> dict[str, Any]:
    """要落进 meta.json 的参数快照。

    请求值与实际值都记：不一致本身就是有用的信息（供应商按档位取整了），只记一个就看不出来。
    negative_prompt 单独记一份并标明有没有真的生效，免得事后把「模型不支持」误当成「写了没
    用」。
    """
    return {
        "driver": candidate.driver,
        "provider": candidate.provider_code,
        "model": candidate.model_id,
        "endpoint": candidate.endpoint,
        "request": _scrub(payload),
        "negative_prompt": negative_prompt,
        "negative_prompt_applied": bool(negative_prompt and negative_field(candidate)),
        "requested_size": f"{requested[0]}x{requested[1]}",
        "actual_size": f"{actual[0]}x{actual[1]}",
        "references": references,
        "latency_ms": latency_ms,
    }


def _scrub(value: Any) -> Any:
    """把请求体里的内联参考图换成一句说明。

    几 MB 的 base64 写进 meta.json 会让这个文件没法用眼睛看，而它的读者首先是人。
    """
    if isinstance(value, Mapping):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str) and value.startswith("data:"):
        return f"<内联参考图 {len(value)} 字符>"
    return value
