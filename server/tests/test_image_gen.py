"""生图驱动：两家协议的请求体、图的下载与量尺、失败分类。

这一层是真的打网络的地方，用 respx 拦下来。要钉的是三件事：**平台字段压得住 provider 配
置**（一行 extra_body 换掉模型或尺寸，图与 meta.json 记的就不是一回事）、**图一定拿成字节**
（签名地址明天就失效）、**尺寸现场量**（`size` 是意图，供应商会按档位取整）。
"""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from atelier.providers import image_gen
from atelier.providers.base import Candidate, ProviderError, RetryableError

ARK_URL = "https://ark.invalid/api/v3/images/generations"
DS_URL = "https://dashscope.invalid/api/v1/services/aigc/multimodal-generation/generation"
IMAGE_URL = "https://oss.invalid/signed/one.png"


def candidate(**kwargs: object) -> Candidate:
    defaults: dict[str, object] = {
        "provider_model_id": 1,
        "provider_code": "ark",
        "provider_name": "方舟",
        "model_id": "doubao-seedream-5.0-lite",
        "driver": image_gen.ARK,
        "endpoint": ARK_URL,
        "api_key": "sk-test",
        "priority": 100,
        "sort_no": 0,
    }
    defaults.update(kwargs)
    return Candidate(**defaults)  # type: ignore[arg-type]


def dashscope(**kwargs: object) -> Candidate:
    return candidate(
        provider_code="bailian",
        provider_name="百炼",
        model_id="qwen-image-2.0",
        driver=image_gen.DASHSCOPE,
        endpoint=DS_URL,
        **kwargs,
    )


def png_bytes(width: int = 64, height: int = 32, kind: str = "PNG") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format=kind)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #


def test_方舟平铺一层且要地址() -> None:
    payload = image_gen.build_payload(candidate(), "红瞳", width=2048, height=2048)

    assert payload["model"] == "doubao-seedream-5.0-lite"
    assert payload["prompt"] == "红瞳"
    assert payload["size"] == "2048x2048"
    assert payload["response_format"] == "url"
    assert payload["watermark"] is False


def test_百炼把提示词塞进消息且尺寸用星号() -> None:
    """写成 x 会被百炼判成非法尺寸。"""
    payload = image_gen.build_payload(dashscope(), "红瞳", width=1024, height=768)

    content = payload["input"]["messages"][0]["content"]
    assert content == [{"text": "红瞳"}]
    assert payload["parameters"]["size"] == "1024*768"
    assert payload["parameters"]["n"] == 1


def test_模型配置改不动平台字段() -> None:
    params = {"extra_body": {"model": "别的模型", "size": "512x512", "steps": 40}}

    payload = image_gen.build_payload(candidate(params=params), "红瞳", width=2048, height=2048)

    assert payload["model"] == "doubao-seedream-5.0-lite"
    assert payload["size"] == "2048x2048"
    assert payload["steps"] == 40


def test_方舟不支持负向提示词就不塞进正向() -> None:
    """正向里写「不要背景」，扩散模型往往反而把背景画出来。"""
    payload = image_gen.build_payload(
        candidate(), "红瞳", negative_prompt="background, watermark", width=512, height=512
    )

    assert image_gen.negative_field(candidate()) is None
    assert "background" not in payload["prompt"]
    assert "negative_prompt" not in payload


def test_百炼默认支持负向提示词() -> None:
    payload = image_gen.build_payload(
        dashscope(), "红瞳", negative_prompt="background", width=512, height=512
    )

    assert payload["parameters"]["negative_prompt"] == "background"


def test_档位只在明确配了才用() -> None:
    """档位会把非正方形的比例抹掉，所以不作为默认。"""
    assert image_gen.ark_size(candidate(), 1024, 768) == "1024x768"
    assert image_gen.ark_size(candidate(params={"size_style": "tier"}), 2048, 2048) == "2K"
    assert image_gen.ark_size(candidate(params={"size_style": "tier"}), 4096, 4096) == "4K"


def test_本地参考图转成内联字节(tmp_path: Path) -> None:
    path = tmp_path / "ref.png"
    path.write_bytes(png_bytes())

    ref = image_gen.reference_ref(path)

    assert ref.startswith("data:image/png;base64,")
    assert image_gen.reference_ref(IMAGE_URL) == IMAGE_URL


def test_参考图不在就不发出去(tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="参考图不存在"):
        image_gen.reference_ref(tmp_path / "nope.png")


# --------------------------------------------------------------------------- #
# 一次调用
# --------------------------------------------------------------------------- #


@respx.mock
def test_方舟把签名地址上的图取成字节() -> None:
    raw = png_bytes(96, 64)
    respx.post(ARK_URL).respond(
        json={"data": [{"url": IMAGE_URL}]},
        headers={"x-ratelimit-remaining-requests": "12"},
    )
    respx.get(IMAGE_URL).respond(content=raw)

    reply = image_gen.generate(candidate(), "红瞳", width=2048, height=2048)

    assert reply.data == raw
    assert reply.suffix == ".png"
    assert reply.remaining == 12


@respx.mock
def test_尺寸按图本身量而不是按请求值() -> None:
    """供应商按自己支持的档位取整，拿请求值判断会把走形的图放过去。"""
    respx.post(ARK_URL).respond(json={"data": [{"url": IMAGE_URL}]})
    respx.get(IMAGE_URL).respond(content=png_bytes(1024, 768))

    reply = image_gen.generate(candidate(), "红瞳", width=2048, height=2048)

    assert reply.size_text == "1024x768"
    assert reply.params["requested_size"] == "2048x2048"
    assert reply.params["actual_size"] == "1024x768"


@respx.mock
def test_内联base64也收() -> None:
    raw = png_bytes()
    inline = base64.b64encode(raw).decode()
    respx.post(ARK_URL).respond(json={"data": [{"b64_json": inline}]})

    reply = image_gen.generate(candidate(), "红瞳")

    assert reply.data == raw


@respx.mock
def test_百炼从output里取图() -> None:
    raw = png_bytes()
    respx.post(DS_URL).respond(
        json={"output": {"choices": [{"message": {"content": [{"image": IMAGE_URL}]}}]}}
    )
    respx.get(IMAGE_URL).respond(content=raw)

    reply = image_gen.generate(dashscope(), "红瞳")

    assert reply.data == raw


@respx.mock
def test_百炼用200带code报错也算失败() -> None:
    respx.post(DS_URL).respond(json={"code": "InvalidParameter", "message": "size 不合法"})

    with pytest.raises(ProviderError, match="size 不合法"):
        image_gen.generate(dashscope(), "红瞳")


@respx.mock
def test_图取不下来按可重试处理() -> None:
    """签名地址过期、对象存储抖动，换个候选重出一张就能绕开。"""
    respx.post(ARK_URL).respond(json={"data": [{"url": IMAGE_URL}]})
    respx.get(IMAGE_URL).respond(403)

    with pytest.raises(RetryableError, match="没取下来"):
        image_gen.generate(candidate(), "红瞳")


@respx.mock
def test_连不上也是可重试() -> None:
    respx.post(ARK_URL).mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(RetryableError):
        image_gen.generate(candidate(), "红瞳")


@respx.mock
def test_不是图的字节直接拒收() -> None:
    """存进素材库的必须是能打开的图。"""
    respx.post(ARK_URL).respond(json={"data": [{"url": IMAGE_URL}]})
    respx.get(IMAGE_URL).respond(content=b"not an image at all")

    with pytest.raises(ProviderError, match="不是能识别的图片"):
        image_gen.generate(candidate(), "红瞳")


@respx.mock
def test_一张图都没返回就报清楚() -> None:
    respx.post(ARK_URL).respond(json={"data": []})

    with pytest.raises(ProviderError, match="没有返回图片"):
        image_gen.generate(candidate(), "红瞳")


def test_绑错驱动不发请求() -> None:
    with pytest.raises(ProviderError, match="不是生图驱动"):
        image_gen.generate(candidate(driver="openai_compat"), "红瞳")


def test_空提示词不发请求() -> None:
    with pytest.raises(ProviderError, match="prompt 是空的"):
        image_gen.generate(candidate(), "   ")


# --------------------------------------------------------------------------- #
# 参数快照
# --------------------------------------------------------------------------- #


@respx.mock
def test_快照记下真实情况且不含密钥(tmp_path: Path) -> None:
    ref = tmp_path / "ref.png"
    ref.write_bytes(png_bytes())
    respx.post(DS_URL).respond(
        json={"output": {"choices": [{"message": {"content": [{"image": IMAGE_URL}]}}]}}
    )
    respx.get(IMAGE_URL).respond(content=png_bytes())

    reply = image_gen.generate(
        dashscope(), "红瞳", negative_prompt="background", references=[ref], seed=7
    )

    params = reply.params
    assert params["model"] == "qwen-image-2.0"
    assert params["negative_prompt"] == "background"
    assert params["negative_prompt_applied"] is True
    assert params["references"] == 1
    assert "sk-test" not in str(params)
    # 内联参考图换成一句说明，免得几 MB 的 base64 灌进 meta.json
    content = params["request"]["input"]["messages"][0]["content"]
    assert content[0]["image"].startswith("<内联参考图")


@respx.mock
def test_不支持负向时快照标明没生效() -> None:
    """免得事后把「模型不支持」误当成「写了没用」。"""
    respx.post(ARK_URL).respond(json={"data": [{"url": IMAGE_URL}]})
    respx.get(IMAGE_URL).respond(content=png_bytes())

    reply = image_gen.generate(candidate(), "红瞳", negative_prompt="background")

    assert reply.params["negative_prompt"] == "background"
    assert reply.params["negative_prompt_applied"] is False
