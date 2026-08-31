"""插件管理接口：列插件、触发安装、查安装进度。

安装是后台线程干的活（见 `plugins.py`），这里三个端点都秒回：install 只是把线程点着，
真进度靠前端轮询 GET 拿。状态存进程内存，不碰任何库。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from atelier import plugins

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


class PluginOut(BaseModel):
    id: str
    name: str
    description: str
    installed: bool
    running: bool
    progress: int
    eta_seconds: int | None = None
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_bytes: int | None = None
    message: str


@router.get("", response_model=list[PluginOut])
def list_all() -> list[PluginOut]:
    return [PluginOut(**one) for one in plugins.list_plugins()]


@router.get("/{plugin_id}", response_model=PluginOut)
def read_one(plugin_id: str) -> PluginOut:
    try:
        return PluginOut(**plugins.plugin_status(plugin_id))
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"没有这个插件：{plugin_id}"
        ) from exc


@router.post("/{plugin_id}/install", response_model=PluginOut)
def install(plugin_id: str) -> PluginOut:
    try:
        return PluginOut(**plugins.start_install(plugin_id))
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"没有这个插件：{plugin_id}"
        ) from exc
