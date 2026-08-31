"""插件管理接口。不触网：下载函数被替换成「在目标目录造个 model.bin」。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atelier import plugins, voice

PLUGIN_ID = "whisper-large-v3"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """模型目录指到临时目录，安装状态每例清零，免得相互串。"""
    monkeypatch.setattr(voice, "MODEL_DIR", tmp_path / "whisper-large-v3")
    plugins._reset_for_tests()
    yield
    plugins._reset_for_tests()


def _fake_download_ok(*, repo_id: str, target_dir, on_progress, ignore=()) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "model.bin").write_bytes(b"fake-model")
    on_progress(10, 10)


def test_列插件默认没装(client: TestClient) -> None:
    response = client.get("/api/plugins")

    assert response.status_code == 200
    body = response.json()
    assert any(one["id"] == PLUGIN_ID for one in body)
    voice_plugin = next(one for one in body if one["id"] == PLUGIN_ID)
    assert voice_plugin["installed"] is False
    assert voice_plugin["running"] is False


def test_安装跑完转为已装(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins, "_download_hf_model", _fake_download_ok)

    started = client.post(f"/api/plugins/{PLUGIN_ID}/install")
    assert started.status_code == 200

    plugins._join_for_tests(PLUGIN_ID)

    status = client.get(f"/api/plugins/{PLUGIN_ID}").json()
    assert status["installed"] is True
    assert status["running"] is False
    assert status["progress"] == 100


def test_下载失败把原因记进状态(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**kwargs) -> None:
        raise RuntimeError("镜像连不上")

    monkeypatch.setattr(plugins, "_download_hf_model", _boom)

    client.post(f"/api/plugins/{PLUGIN_ID}/install")
    plugins._join_for_tests(PLUGIN_ID)

    status = client.get(f"/api/plugins/{PLUGIN_ID}").json()
    assert status["installed"] is False
    assert status["running"] is False
    assert "镜像连不上" in status["message"]


def test_未知插件回四百零四(client: TestClient) -> None:
    assert client.get("/api/plugins/nope").status_code == 404
    assert client.post("/api/plugins/nope/install").status_code == 404
