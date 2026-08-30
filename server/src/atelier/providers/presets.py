"""新建 provider 时的预设：把型号目录按套餐归成一份可直接落库的配置。

一家供应商的一个套餐，端点、driver、鉴权头、模型清单与计量口径都是固定的事实，只有 key、
优先级与额度数字因人而异。这些事实住在配置库的 `model_catalog`（随 seeds 进 Git），所以
换套餐、加模型改 `seeds/model_catalog.json` 重新灌一次即可，不用改代码也不用动 UI。

预设只是「表单的初值」：交上来的仍是一份完整的 ProviderIn，落库路径与手工新建完全同一条。
这样一来预设错了顶多是初值不对，用户当场能改，不会有一条绕过校验的暗路。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from atelier.db.config_models import ModelCatalog


@dataclass(frozen=True, slots=True)
class PresetModel:
    """预设里的一个模型。`limit_kind` 与 `default_period` 决定表单上那个额度数字的口径。"""

    model_id: str
    capabilities: list[str]
    driver: str
    api_path: str | None
    limit_kind: str
    default_period: str
    remark: str | None


@dataclass(frozen=True, slots=True)
class Preset:
    """一个套餐一份预设。`code` 只是建议值——同一套餐开两个账号就得各起一个。"""

    code: str
    vendor: str
    plan: str
    label: str
    base_url: str
    driver: str
    auth_style: str
    key_prefix: str | None
    models: list[PresetModel] = field(default_factory=list)


def _label(vendor: str, plan: str) -> str:
    return f"{vendor} · {plan}" if plan else vendor


def _base_url(rows: list[ModelCatalog]) -> str:
    for row in rows:
        if row.base_url:
            return row.base_url
    return ""


def _provider_driver(rows: list[ModelCatalog]) -> str:
    """provider 级 driver 取套餐内出现最多的那个。

    同一套餐里生图与文本的 driver 常常不同（方舟 Agent Plan 就是 ark_image 与 ark_video
    混着），所以每个模型都会带上自己的 driver，这里选谁只影响「以后手工新增模型」的默认值。
    """
    tally: dict[str, int] = {}
    for row in rows:
        tally[row.driver] = tally.get(row.driver, 0) + 1
    return max(tally, key=lambda name: (tally[name], name)) if tally else "openai_compat"


def list_presets(session: Session) -> list[Preset]:
    """配置库里的全部预设，按 `preset_code` 归组。

    没写 `preset_code` 的行不成预设：它只是型号速查表里的一条参考，凑不出一个能落库的账号。
    """
    rows = session.scalars(
        select(ModelCatalog).order_by(
            ModelCatalog.vendor, ModelCatalog.plan, ModelCatalog.id, ModelCatalog.model_id
        )
    ).all()

    grouped: dict[str, list[ModelCatalog]] = {}
    for row in rows:
        if not row.preset_code:
            continue
        grouped.setdefault(row.preset_code, []).append(row)

    presets: list[Preset] = []
    for code, group in grouped.items():
        head = group[0]
        presets.append(
            Preset(
                code=code,
                vendor=head.vendor,
                plan=head.plan,
                label=_label(head.vendor, head.plan),
                base_url=_base_url(group),
                driver=_provider_driver(group),
                auth_style=head.auth_style,
                key_prefix=head.key_prefix,
                models=[
                    PresetModel(
                        model_id=row.model_id,
                        capabilities=list(row.capabilities),
                        driver=row.driver,
                        api_path=row.api_path,
                        limit_kind=row.limit_kind,
                        default_period=row.default_period,
                        remark=row.remark,
                    )
                    for row in group
                ],
            )
        )
    return presets
