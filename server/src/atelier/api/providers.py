"""provider 设置页与额度看板的 HTTP 接口。

响应体一律不含明文 api_key（见 schemas 的铁律）。唯一例外是导出接口显式带
`include_keys=true`——那是用户主动要把整套配置交给别人。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Response, status

from atelier.api import provider_ops as ops
from atelier.api.deps import RuntimeDb
from atelier.api.portable import build_portable, parse_portable
from atelier.api.schemas import (
    ImportRequest,
    ImportResult,
    ModelIn,
    ModelOut,
    ProviderIn,
    ProviderOut,
    ProviderPatch,
    UsageBoardOut,
)

router = APIRouter(prefix="/api/providers", tags=["providers"])


# --------------------------------------------------------------------------- #
# 额度看板与整包导入导出：路径写在 /{code} 之前，否则会被当成 code 吃掉
# --------------------------------------------------------------------------- #


@router.get("/usage", response_model=UsageBoardOut)
def usage_board(
    session: RuntimeDb,
    refresh: bool = Query(default=False, description="true 时向远程用量服务对账，慢但准"),
) -> UsageBoardOut:
    return UsageBoardOut(items=ops.usage_board(session, refresh=refresh))


@router.get("/export")
def export_config(
    session: RuntimeDb,
    include_keys: bool = Query(
        default=False, description="带上明文 api_key。默认不带，导出的是可分享的模板"
    ),
) -> dict[str, Any]:
    payloads = [ops.to_provider_in(session, provider) for provider in ops.all_providers(session)]
    return build_portable(payloads, include_keys=include_keys)


@router.post("/import", response_model=ImportResult)
def import_config(session: RuntimeDb, payload: ImportRequest) -> ImportResult:
    providers, warnings = parse_portable(payload.providers)
    return ops.import_providers(session, providers, mode=payload.mode, warnings=warnings)


# --------------------------------------------------------------------------- #
# provider
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[ProviderOut])
def list_providers(session: RuntimeDb) -> list[ProviderOut]:
    return [ops.to_provider_out(session, p) for p in ops.all_providers(session)]


@router.post("", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
def create_provider(session: RuntimeDb, payload: ProviderIn) -> ProviderOut:
    provider = ops.create_provider(session, payload)
    return ops.to_provider_out(session, provider)


@router.get("/{code}", response_model=ProviderOut)
def read_provider(session: RuntimeDb, code: str) -> ProviderOut:
    return ops.to_provider_out(session, ops.get_provider(session, code))


@router.patch("/{code}", response_model=ProviderOut)
def update_provider(session: RuntimeDb, code: str, patch: ProviderPatch) -> ProviderOut:
    provider = ops.update_provider(session, ops.get_provider(session, code), patch)
    return ops.to_provider_out(session, provider)


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(session: RuntimeDb, code: str) -> Response:
    """连带删掉该账号下的模型、额度、用量镜像与 Agent 绑定。"""
    ops.delete_provider(session, ops.get_provider(session, code))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# provider 下的模型
# --------------------------------------------------------------------------- #


@router.post("/{code}/models", response_model=ModelOut, status_code=status.HTTP_201_CREATED)
def add_model(session: RuntimeDb, code: str, payload: ModelIn) -> ModelOut:
    """同名模型视为更新，不报冲突——设置页反复保存同一条是常态。"""
    provider = ops.get_provider(session, code)
    model = ops.upsert_model(session, provider, payload)
    return ops.to_model_out(session, model)


@router.put("/{code}/models/{provider_model_id}", response_model=ModelOut)
def update_model(
    session: RuntimeDb, code: str, provider_model_id: int, payload: ModelIn
) -> ModelOut:
    model = ops.get_model(session, code, provider_model_id)
    return ops.to_model_out(session, ops.update_model(session, model, payload))


@router.delete("/{code}/models/{provider_model_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model(session: RuntimeDb, code: str, provider_model_id: int) -> Response:
    ops.delete_model(session, ops.get_model(session, code, provider_model_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{code}/models/{provider_model_id}/agents", response_model=ModelOut)
def bind_agents(
    session: RuntimeDb,
    code: str,
    provider_model_id: int,
    agent_codes: Annotated[list[str], Body()],
) -> ModelOut:
    """整组替换该模型绑定的 Agent。"""
    model = ops.get_model(session, code, provider_model_id)
    ops.replace_agents(session, model, agent_codes)
    session.commit()
    return ops.to_model_out(session, model)


# --------------------------------------------------------------------------- #
# 运行态复位
# --------------------------------------------------------------------------- #


@router.delete("/{code}/models/{provider_model_id}/breaker", status_code=status.HTTP_204_NO_CONTENT)
def clear_breaker(session: RuntimeDb, code: str, provider_model_id: int) -> Response:
    """手动放行熔断：确认对方恢复了，不必干等窗口走完。"""
    ops.get_model(session, code, provider_model_id)
    ops.clear_breaker(session, provider_model_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{code}/models/{provider_model_id}/usage")
def reset_usage(
    session: RuntimeDb,
    code: str,
    provider_model_id: int,
    limit_kind: str | None = Query(default=None),
) -> dict[str, int]:
    """清掉本地用量镜像，让本机重新与远程用量服务对账。"""
    ops.get_model(session, code, provider_model_id)
    return {"cleared": ops.reset_usage(session, provider_model_id, limit_kind)}
