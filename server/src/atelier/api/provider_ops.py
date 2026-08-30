"""provider 配置的读写与序列化。

Agent 绑定与额度都按「整组替换」处理：前端交上来的那一份就是全量，比逐条 diff 少一半
出错机会。删 provider 会连带删掉它的模型、额度、用量与绑定（外键级联），所以删除接口
要求前端确认。

「不存在」与「冲突」用包根的 `atelier.errors`，与项目层共用同一套语义与 HTTP 映射。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from atelier.api.schemas import (
    BreakerOut,
    BudgetOut,
    ImportResult,
    LimitIn,
    LimitOut,
    ModelIn,
    ModelOut,
    ModelUsageOut,
    ProviderIn,
    ProviderOut,
    ProviderPatch,
)
from atelier.db.runtime_models import (
    ModelLimit,
    Provider,
    ProviderAgentModel,
    ProviderModel,
    UsageCounter,
)
from atelier.errors import Conflict, NotFound
from atelier.providers import period as period_mod
from atelier.providers import router, usage
from atelier.providers.usage_client import mask_key

# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #


def all_providers(session: Session) -> list[Provider]:
    return list(session.scalars(select(Provider).order_by(Provider.priority, Provider.code)).all())


def get_provider(session: Session, code: str) -> Provider:
    provider = session.get(Provider, code)
    if provider is None:
        raise NotFound(f"provider {code} 不存在")
    return provider


def get_model(session: Session, code: str, provider_model_id: int) -> ProviderModel:
    model = session.get(ProviderModel, provider_model_id)
    if model is None or model.provider_code != code:
        raise NotFound(f"provider {code} 下没有 id={provider_model_id} 的模型")
    return model


def agents_of(session: Session, provider_model_id: int) -> list[str]:
    return list(
        session.scalars(
            select(ProviderAgentModel.agent_code)
            .where(ProviderAgentModel.provider_model_id == provider_model_id)
            .order_by(ProviderAgentModel.agent_code)
        ).all()
    )


def to_limit_out(row: ModelLimit) -> LimitOut:
    return LimitOut(
        id=row.id,
        limit_kind=row.limit_kind,
        max_value=row.max_value,
        group_name=row.group_name,
        period_expr=row.period_expr,
        window_text=period_mod.window_text(row.period_expr),
    )


def to_model_out(session: Session, model: ProviderModel) -> ModelOut:
    return ModelOut(
        id=model.id,
        provider_code=model.provider_code,
        model_id=model.model_id,
        capabilities=list(model.capabilities or []),
        driver=model.driver,
        effective_driver=model.effective_driver,
        api_path=model.api_path,
        endpoint=model.endpoint(),
        enabled=model.enabled,
        sort_no=model.sort_no,
        params=dict(model.params or {}),
        remark=model.remark,
        agents=agents_of(session, model.id),
        limits=[to_limit_out(row) for row in sorted(model.limits, key=lambda r: r.limit_kind)],
    )


def to_provider_out(session: Session, provider: Provider) -> ProviderOut:
    return ProviderOut(
        code=provider.code,
        name=provider.name,
        base_url=provider.base_url,
        api_key_mask=mask_key(provider.api_key),
        has_key=bool(provider.api_key.strip()),
        enabled=provider.enabled,
        priority=provider.priority,
        driver=provider.driver,
        auth_style=provider.auth_style,
        verify_ssl=provider.verify_ssl,
        remark=provider.remark,
        models=[
            to_model_out(session, model)
            for model in sorted(provider.models, key=lambda m: (m.sort_no, m.model_id))
        ],
    )


def to_provider_in(session: Session, provider: Provider) -> ProviderIn:
    """反向拼成入参形状，供导出复用同一套字段定义。含明文 key，只给导出接口用。"""
    return ProviderIn(
        code=provider.code,
        name=provider.name,
        base_url=provider.base_url,
        api_key=provider.api_key,
        enabled=provider.enabled,
        priority=provider.priority,
        driver=provider.driver,
        auth_style=provider.auth_style,
        verify_ssl=provider.verify_ssl,
        remark=provider.remark,
        models=[
            ModelIn(
                model_id=model.model_id,
                capabilities=list(model.capabilities or []),
                driver=model.driver,
                api_path=model.api_path,
                enabled=model.enabled,
                sort_no=model.sort_no,
                params=dict(model.params or {}),
                remark=model.remark,
                agents=agents_of(session, model.id),
                limits=[
                    LimitIn(
                        limit_kind=row.limit_kind,
                        max_value=row.max_value,
                        group_name=row.group_name,
                        period_expr=row.period_expr,
                    )
                    for row in sorted(model.limits, key=lambda r: r.limit_kind)
                ],
            )
            for model in sorted(provider.models, key=lambda m: (m.sort_no, m.model_id))
        ],
    )


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #


def create_provider(session: Session, payload: ProviderIn) -> Provider:
    if session.get(Provider, payload.code) is not None:
        raise Conflict(f"provider {payload.code} 已存在")

    provider = Provider(
        code=payload.code,
        name=payload.name or payload.code,
        base_url=payload.base_url,
        api_key=payload.api_key,
        enabled=payload.enabled,
        priority=payload.priority,
        driver=payload.driver,
        auth_style=payload.auth_style,
        verify_ssl=payload.verify_ssl,
        remark=payload.remark,
    )
    session.add(provider)
    session.flush()

    for model in payload.models:
        upsert_model(session, provider, model)
    session.commit()
    return provider


def update_provider(session: Session, provider: Provider, patch: ProviderPatch) -> Provider:
    for field, value in patch.model_dump(exclude_unset=True).items():
        setattr(provider, field, value)
    session.commit()
    return provider


def delete_provider(session: Session, provider: Provider) -> None:
    session.delete(provider)
    session.commit()


def upsert_model(session: Session, provider: Provider, payload: ModelIn) -> ProviderModel:
    """按 model_id 认账：有就更新，没有就新增。"""
    model = session.scalars(
        select(ProviderModel).where(
            ProviderModel.provider_code == provider.code,
            ProviderModel.model_id == payload.model_id,
        )
    ).one_or_none()

    if model is None:
        model = ProviderModel(provider_code=provider.code, model_id=payload.model_id)
        session.add(model)

    model.capabilities = list(payload.capabilities)
    model.driver = payload.driver
    model.api_path = payload.api_path
    model.enabled = payload.enabled
    model.sort_no = payload.sort_no
    model.params = dict(payload.params)
    model.remark = payload.remark
    session.flush()

    replace_agents(session, model, payload.agents)
    replace_limits(session, model, payload.limits)
    session.commit()
    # session 是 expire_on_commit=False，limits 走 relationship 缓存，不主动作废就会返回旧额度
    session.expire(model, ["limits"])
    return model


def delete_model(session: Session, model: ProviderModel) -> None:
    session.delete(model)
    session.commit()


def update_model(session: Session, model: ProviderModel, payload: ModelIn) -> ProviderModel:
    """按 id 改一条模型，可以改 model_id（同 provider 下不能撞名）。"""
    if payload.model_id != model.model_id:
        clash = session.scalars(
            select(ProviderModel).where(
                ProviderModel.provider_code == model.provider_code,
                ProviderModel.model_id == payload.model_id,
            )
        ).one_or_none()
        if clash is not None:
            raise Conflict(f"{model.provider_code} 下已有模型 {payload.model_id}")
        model.model_id = payload.model_id

    model.capabilities = list(payload.capabilities)
    model.driver = payload.driver
    model.api_path = payload.api_path
    model.enabled = payload.enabled
    model.sort_no = payload.sort_no
    model.params = dict(payload.params)
    model.remark = payload.remark
    session.flush()

    replace_agents(session, model, payload.agents)
    replace_limits(session, model, payload.limits)
    session.commit()
    session.expire(model, ["limits"])
    return model


def replace_agents(session: Session, model: ProviderModel, agent_codes: list[str]) -> None:
    """整组替换绑定。留下已有行不动，避免把 enabled=false 的人工禁用状态刷掉。"""
    wanted = {code.strip() for code in agent_codes if code.strip()}
    existing = {
        row.agent_code: row
        for row in session.scalars(
            select(ProviderAgentModel).where(ProviderAgentModel.provider_model_id == model.id)
        ).all()
    }

    for agent_code in wanted - set(existing):
        session.add(ProviderAgentModel(agent_code=agent_code, provider_model_id=model.id))
    for agent_code in set(existing) - wanted:
        session.delete(existing[agent_code])
    session.flush()


def replace_limits(session: Session, model: ProviderModel, limits: list[LimitIn]) -> None:
    """整组替换额度。max_value=0 视为不限量，直接删掉这条配置。"""
    wanted = {row.limit_kind: row for row in limits if row.max_value > 0}
    existing = {
        row.limit_kind: row
        for row in session.scalars(
            select(ModelLimit).where(ModelLimit.provider_model_id == model.id)
        ).all()
    }

    for kind, payload in wanted.items():
        row = existing.get(kind)
        if row is None:
            row = ModelLimit(provider_model_id=model.id, limit_kind=kind)
            session.add(row)
        row.max_value = payload.max_value
        row.group_name = payload.group_name
        row.period_expr = payload.period_expr
    for stale in set(existing) - set(wanted):
        session.delete(existing[stale])
    session.flush()


# --------------------------------------------------------------------------- #
# 整包导入
# --------------------------------------------------------------------------- #


def import_providers(
    session: Session, payloads: list[ProviderIn], *, mode: str, warnings: list[str]
) -> ImportResult:
    """灌入整包配置。

    merge：包里有的建/更新，包里没提到的留着不动。
    replace：先清空全部 provider——用量与绑定一起消失，只在「换一台机器重装」时用。
    """
    removed: list[str] = []
    if mode == "replace":
        for provider in all_providers(session):
            if provider.code not in {p.code for p in payloads}:
                removed.append(provider.code)
                session.delete(provider)
        session.flush()

    created: list[str] = []
    updated: list[str] = []
    models = bindings = limits = 0

    for payload in payloads:
        target = session.get(Provider, payload.code)
        if target is None:
            target = Provider(code=payload.code, name=payload.name, base_url=payload.base_url)
            session.add(target)
            created.append(payload.code)
        else:
            updated.append(payload.code)

        target.name = payload.name or payload.code
        target.base_url = payload.base_url
        target.enabled = payload.enabled
        target.priority = payload.priority
        target.driver = payload.driver
        target.auth_style = payload.auth_style
        target.verify_ssl = payload.verify_ssl
        target.remark = payload.remark
        # 导出模板不带 key，导入时不能把本机已配好的 key 抹成空
        if payload.api_key.strip():
            target.api_key = payload.api_key
        elif not (target.api_key or "").strip():
            warnings.append(f"{payload.code}: 没带 api_key，导入后请在设置页补上")
        session.flush()

        for model_payload in payload.models:
            upsert_model(session, target, model_payload)
            models += 1
            bindings += len(model_payload.agents)
            limits += len([row for row in model_payload.limits if row.max_value > 0])

    session.commit()
    return ImportResult(
        created=created,
        updated=updated,
        removed=removed,
        models=models,
        bindings=bindings,
        limits=limits,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- #
# 额度看板
# --------------------------------------------------------------------------- #


def usage_board(session: Session, *, refresh: bool = False) -> list[ModelUsageOut]:
    """每个候选当前窗口的用量与熔断状态。

    refresh=True 才向远程用量服务对账（一个候选一次往返），默认只读本地镜像。
    """
    items: list[ModelUsageOut] = []
    for provider in all_providers(session):
        for model in sorted(provider.models, key=lambda m: (m.sort_no, m.model_id)):
            budgets = [
                _budget_out(session, model, row, refresh=refresh)
                for row in sorted(model.limits, key=lambda r: r.limit_kind)
            ]
            items.append(
                ModelUsageOut(
                    provider_model_id=model.id,
                    provider_code=provider.code,
                    provider_name=provider.name,
                    provider_enabled=provider.enabled,
                    model_id=model.model_id,
                    enabled=model.enabled,
                    has_key=bool(provider.api_key.strip()),
                    priority=provider.priority,
                    agents=agents_of(session, model.id),
                    budgets=budgets,
                    breaker=_breaker_out(session, model.id),
                )
            )
    return items


def _budget_out(
    session: Session, model: ProviderModel, limit_row: ModelLimit, *, refresh: bool
) -> BudgetOut:
    seen = (
        usage.peek(session, model, limit_row.limit_kind)
        if refresh
        else usage.local_peek(session, model, limit_row.limit_kind)
    )
    return BudgetOut(
        limit_kind=seen.limit_kind,
        limit=seen.limit,
        used=seen.used,
        remaining=seen.remaining,
        available=seen.available,
        window_key=seen.window_key,
        window_text=period_mod.window_text(limit_row.period_expr),
        period_expr=limit_row.period_expr,
        group_name=limit_row.group_name,
        source=seen.source,
        exhausted=seen.exhausted_at is not None,
        unlimited=seen.unlimited,
    )


def _breaker_out(session: Session, provider_model_id: int) -> BreakerOut | None:
    if not router.is_open(session, provider_model_id):
        return None
    breaker = router.breaker_of(session, provider_model_id)
    if breaker is None:
        return None
    return BreakerOut(
        open_until=breaker.open_until.isoformat(),
        fail_count=breaker.fail_count,
        last_reason=breaker.last_reason,
    )


def clear_breaker(session: Session, provider_model_id: int) -> None:
    """手动放行：知道对方已经恢复了，不必干等熔断窗口走完。"""
    router.close_breaker(session, provider_model_id)


def reset_usage(session: Session, provider_model_id: int, limit_kind: str | None = None) -> int:
    """清掉本地用量镜像。远程用量服务才是真相，这里只是让本机重新对账。"""
    stmt = select(UsageCounter).where(UsageCounter.provider_model_id == provider_model_id)
    if limit_kind is not None:
        stmt = stmt.where(UsageCounter.limit_kind == limit_kind)
    rows = list(session.scalars(stmt).all())
    for row in rows:
        session.delete(row)
    session.commit()
    return len(rows)
