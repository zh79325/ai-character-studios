"""配置库（db/config.db，进 Git）表定义。

只放不含凭证的公共数据，全部由 seeds/ 经 seed.py 幂等灌入，不在 UI 直接改表。
提示词不入库：工程级提示词是代码资产，只住在 atelier/prompts/，运行时直读文件；
项目级的提示词增量属于用户数据，落日志库。
禁止与日志库 join，跨库引用只存 code 字符串。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ConfigBase(DeclarativeBase):
    """配置库独立 Base，与日志库 metadata 完全隔离。"""

    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


class ModelCatalog(ConfigBase):
    """常见供应商、套餐与模型清单，既是填 provider 配置时的候选提示，也是新建账号的预设来源。

    同一供应商的不同套餐（如方舟 Coding Plan / Agent Plan）key 与端点完全隔离，
    因此 plan 参与唯一键；同一套餐内不同模型的 driver 与 api_path 也可不同。

    `preset_code` 把同一套餐的几行归成一个预设：新建账号时选它，端点、driver、模型清单
    与计量口径一次填齐，用户只补 key、优先级与额度数字。它不参与路由，路由只看 providers。
    """

    __tablename__ = "model_catalog"
    __table_args__ = (UniqueConstraint("vendor", "plan", "model_id", name="uq_model_catalog"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vendor: Mapped[str] = mapped_column(String(64))
    plan: Mapped[str] = mapped_column(String(64), default="")
    preset_code: Mapped[str] = mapped_column(String(64), default="")
    driver: Mapped[str] = mapped_column(String(32))
    model_id: Mapped[str] = mapped_column(String(128))
    capabilities: Mapped[list[str]] = mapped_column(default=list)
    limit_kind: Mapped[str] = mapped_column(String(16), default="tokens")
    default_period: Mapped[str] = mapped_column(String(32), default="day")
    base_url: Mapped[str | None] = mapped_column(String(255), default=None)
    api_path: Mapped[str | None] = mapped_column(String(255), default=None)
    auth_style: Mapped[str] = mapped_column(String(16), default="bearer")
    key_prefix: Mapped[str | None] = mapped_column(String(16), default=None)
    remark: Mapped[str | None] = mapped_column(Text, default=None)


class MeshyAction(ConfigBase):
    """Meshy 预置动作库，按 Category/SubCategory 筛选后传 action_id。"""

    __tablename__ = "meshy_actions"

    action_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(64))
    sub_category: Mapped[str | None] = mapped_column(String(64), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)


class AssetCategory(ConfigBase):
    """素材维度：人物/装备/地图/场景，对应项目下的目录名。"""

    __tablename__ = "asset_categories"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    dir_name: Mapped[str] = mapped_column(String(64))
    sort_no: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class WorkflowDef(ConfigBase):
    """工作流定义：状态清单、跃迁规则、门禁位置。"""

    __tablename__ = "workflow_defs"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    states: Mapped[list[str]] = mapped_column(default=list)
    transitions: Mapped[dict[str, Any]] = mapped_column(default=dict)
    gates: Mapped[dict[str, Any]] = mapped_column(default=dict)
    remark: Mapped[str | None] = mapped_column(Text, default=None)
