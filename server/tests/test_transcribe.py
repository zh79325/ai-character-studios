"""转写接口。真实模型不进测试——加载与转写都被替换掉，只验路由的形状与错误映射。"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from atelier import voice


def _upload(client: TestClient, data: bytes = b"fake-audio"):
    return client.post(
        "/api/transcribe", files={"audio": ("clip.webm", io.BytesIO(data), "audio/webm")}
    )


def test_转写把录音变成文字(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice, "transcribe", lambda audio: "你好世界")

    response = _upload(client)

    assert response.status_code == 200
    assert response.json() == {"text": "你好世界"}


def test_模型没装就提示去装(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(audio: bytes) -> str:
        raise voice.VoiceModelMissing("语音模型还没装：克隆到 xxx")

    monkeypatch.setattr(voice, "transcribe", _missing)

    response = _upload(client)

    assert response.status_code == 503
    assert "还没装" in response.json()["detail"]


def test_空录音回四百(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _empty(audio: bytes) -> str:
        raise ValueError("音频是空的")

    monkeypatch.setattr(voice, "transcribe", _empty)

    response = _upload(client, data=b"")

    assert response.status_code == 400
