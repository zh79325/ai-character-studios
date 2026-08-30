"""provider 配置的整包导入导出。

外部格式沿用参考配置 `provider_agents.json` 的语义（`agents` 映射 + `model_limits`），
这样已经在用那套配置的人可以原样搬进来。本工具多出来的信息（模型级 driver/api_path、
积分单价、一个 Agent 绑多个模型）放在扩展键里，导入时优先读扩展键、缺了就回落参考格式，
因此「导出再导入」是无损的，而「拿别人的参考配置导入」也能跑。

纯函数，不碰数据库：便于单测，也便于将来给 CLI 复用。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from atelier.api.schemas import LimitIn, ModelIn, ProviderIn

# 参考格式里额度写成 max_tokens / max_calls / max_credits
LIMIT_VALUE_KEYS: dict[str, str] = {
    "tokens": "max_tokens",
    "calls": "max_calls",
    "credits": "max_credits",
}
_KIND_BY_KEY = {v: k for k, v in LIMIT_VALUE_KEYS.items()}

# 参考配置里存在但本工具用不上的键：显式告知被忽略，避免用户以为生效了
_KNOWN_KEYS = {
    "name",
    "enabled",
    "priority",
    "driver",
    "base_url",
    "api_key",
    "auth_style",
    "verify_ssl",
    "remark",
    "agents",
    "agent_models",
    "model_limits",
    "models",
}


class PortableError(ValueError):
    """整包配置结构不合法。"""


def parse_portable(raw: Mapping[str, Any]) -> tuple[list[ProviderIn], list[str]]:
    """把整包配置解析成 ProviderIn 列表，附带「哪些字段被忽略了」的提醒。"""
    if not isinstance(raw, Mapping):
        raise PortableError("整包配置必须是 {provider_code: {...}} 的映射")

    warnings: list[str] = []
    providers: list[ProviderIn] = []
    for code, entry in raw.items():
        if not isinstance(entry, Mapping):
            raise PortableError(f"{code}: 配置项必须是键值映射")
        providers.append(_one_provider(str(code), entry, warnings))
    return providers, warnings


def _one_provider(code: str, entry: Mapping[str, Any], warnings: list[str]) -> ProviderIn:
    ignored = sorted(set(entry) - _KNOWN_KEYS)
    if ignored:
        warnings.append(f"{code}: 忽略不支持的字段 {ignored}")

    base_url = str(entry.get("base_url") or "").strip()
    if not base_url:
        raise PortableError(f"{code}: 缺 base_url")

    agent_models = _agent_models(code, entry, warnings)
    limits_by_model = _limits_by_model(code, entry, warnings)
    model_extras = entry.get("models") or {}
    if not isinstance(model_extras, Mapping):
        raise PortableError(f"{code}: models 必须是 {{model_id: {{...}}}} 的映射")

    agents_by_model: dict[str, list[str]] = {}
    for agent_code, model_ids in agent_models.items():
        for model_id in model_ids:
            agents_by_model.setdefault(model_id, []).append(agent_code)

    ordered: list[str] = []
    for model_id in [*model_extras, *agents_by_model, *limits_by_model]:
        if model_id not in ordered:
            ordered.append(model_id)

    models = [
        _one_model(
            model_id,
            extra=model_extras.get(model_id) or {},
            agents=agents_by_model.get(model_id, []),
            limits=limits_by_model.get(model_id, []),
            fallback_sort_no=index,
        )
        for index, model_id in enumerate(ordered)
    ]

    return ProviderIn(
        code=code,
        name=str(entry.get("name") or code),
        base_url=base_url,
        api_key=str(entry.get("api_key") or ""),
        enabled=bool(entry.get("enabled", True)),
        priority=int(entry.get("priority", 100)),
        driver=str(entry.get("driver") or "openai_compat"),
        auth_style=str(entry.get("auth_style") or "bearer"),
        verify_ssl=bool(entry.get("verify_ssl", True)),
        remark=_text_or_none(entry.get("remark")),
        models=models,
    )


def _one_model(
    model_id: str,
    *,
    extra: Mapping[str, Any],
    agents: list[str],
    limits: list[LimitIn],
    fallback_sort_no: int,
) -> ModelIn:
    capabilities = extra.get("capabilities")
    params = extra.get("params")
    return ModelIn(
        model_id=model_id,
        capabilities=[str(c) for c in capabilities] if isinstance(capabilities, list) else ["text"],
        driver=_text_or_none(extra.get("driver")),
        api_path=_text_or_none(extra.get("api_path")),
        enabled=bool(extra.get("enabled", True)),
        sort_no=int(extra.get("sort_no", fallback_sort_no)),
        params=dict(params) if isinstance(params, Mapping) else {},
        remark=_text_or_none(extra.get("remark")),
        agents=agents,
        limits=limits,
    )


def _agent_models(code: str, entry: Mapping[str, Any], warnings: list[str]) -> dict[str, list[str]]:
    """Agent → 模型列表。扩展键 agent_models 优先，回落参考格式的 agents 单值映射。"""
    source = entry.get("agent_models")
    if not isinstance(source, Mapping):
        source = entry.get("agents") or {}
    if not isinstance(source, Mapping):
        raise PortableError(f"{code}: agents 必须是 {{agent_code: model_id}} 的映射")

    result: dict[str, list[str]] = {}
    for agent_code, value in source.items():
        if isinstance(value, str):
            model_ids = [value]
        elif isinstance(value, list):
            model_ids = [str(v) for v in value]
        else:
            warnings.append(f"{code}: Agent {agent_code} 的模型配置无法识别，已跳过")
            continue
        picked = [m for m in model_ids if m.strip()]
        if picked:
            result[str(agent_code)] = picked
    return result


def _limits_by_model(
    code: str, entry: Mapping[str, Any], warnings: list[str]
) -> dict[str, list[LimitIn]]:
    source = entry.get("model_limits") or {}
    if not isinstance(source, Mapping):
        raise PortableError(f"{code}: model_limits 必须是 {{model_id: {{...}}}} 的映射")

    result: dict[str, list[LimitIn]] = {}
    for model_id, spec in source.items():
        if not isinstance(spec, Mapping):
            warnings.append(f"{code}: {model_id} 的额度配置无法识别，已跳过")
            continue
        group_name = str(spec.get("group") or spec.get("group_name") or "default")
        period_expr = str(spec.get("period") or spec.get("period_expr") or "day")
        rows: list[LimitIn] = []
        for key, kind in _KIND_BY_KEY.items():
            if key not in spec:
                continue
            try:
                max_value = int(spec[key])
            except (TypeError, ValueError):
                warnings.append(f"{code}: {model_id} 的 {key} 不是整数，已跳过")
                continue
            rows.append(
                LimitIn(
                    limit_kind=kind,
                    max_value=max(max_value, 0),
                    group_name=group_name,
                    period_expr=period_expr,
                )
            )
        if rows:
            result[str(model_id)] = rows
    return result


def build_portable(providers: list[ProviderIn], *, include_keys: bool) -> dict[str, Any]:
    """反向拼出整包配置。include_keys=False 时 api_key 一律留空，只能当模板用。"""
    out: dict[str, Any] = {}
    for provider in providers:
        agents: dict[str, str] = {}
        agent_models: dict[str, list[str]] = {}
        model_limits: dict[str, dict[str, Any]] = {}
        models: dict[str, dict[str, Any]] = {}

        for model in sorted(provider.models, key=lambda m: (m.sort_no, m.model_id)):
            for agent_code in model.agents:
                agent_models.setdefault(agent_code, []).append(model.model_id)
                agents.setdefault(agent_code, model.model_id)

            if model.limits:
                spec: dict[str, Any] = {}
                for limit in model.limits:
                    spec[LIMIT_VALUE_KEYS[limit.limit_kind]] = limit.max_value
                    spec["group"] = limit.group_name
                    spec["period"] = limit.period_expr
                model_limits[model.model_id] = spec

            models[model.model_id] = {
                "capabilities": model.capabilities,
                "driver": model.driver,
                "api_path": model.api_path,
                "enabled": model.enabled,
                "sort_no": model.sort_no,
                "params": model.params,
                "remark": model.remark,
            }

        entry: dict[str, Any] = {
            "name": provider.name,
            "enabled": provider.enabled,
            "priority": provider.priority,
            "driver": provider.driver,
            "base_url": provider.base_url,
            "api_key": provider.api_key if include_keys else "",
            "auth_style": provider.auth_style,
            "verify_ssl": provider.verify_ssl,
            "remark": provider.remark,
            "agents": agents,
            "agent_models": agent_models,
            "model_limits": model_limits,
            "models": models,
        }
        out[provider.code] = entry
    return out


def _text_or_none(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None
