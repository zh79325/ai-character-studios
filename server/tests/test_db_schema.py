"""三库边界与 provider 三层结构。

config.db 只放公共配置；runtime.db 只放机器级数据（凭证、额度、路由日志、项目索引）；
项目自己的东西（素材、任务、会话、记忆）在项目目录下的 project.db。三套 metadata 不许有交集，
提示词三张表已经彻底从 config.db 移除。
"""

from __future__ import annotations

from atelier.db.config_models import ConfigBase
from atelier.db.project_models import Conversation, ProjectBase
from atelier.db.runtime_models import (
    Provider,
    ProviderModel,
    RuntimeBase,
)

CONFIG_TABLES = {"model_catalog", "meshy_actions", "asset_categories", "workflow_defs"}


def test_config_tables_are_exactly_the_public_four() -> None:
    assert set(ConfigBase.metadata.tables) == CONFIG_TABLES


def test_prompt_tables_are_not_in_any_database() -> None:
    """提示词是代码资产，两个库都不该出现这三张表。"""
    gone = {"agents", "prompt_templates", "negative_presets"}
    assert not gone & set(ConfigBase.metadata.tables)
    assert not gone & set(RuntimeBase.metadata.tables)


def test_project_prompt_extension_tables_live_in_project_db() -> None:
    """项目级增量属于项目自己，跟着目录走，不能留在本机的 runtime.db 里。"""
    tables = {"project_agent_prompts", "project_prompt_snippets"}
    assert tables <= set(ProjectBase.metadata.tables)
    assert not tables & set(RuntimeBase.metadata.tables)


def test_runtime_holds_only_machine_level_tables() -> None:
    """全局库只能有机器级的东西——否则项目拷到另一台机器就会丢东西。"""
    assert set(RuntimeBase.metadata.tables) == {
        "providers",
        "provider_models",
        "provider_agent_models",
        "model_limits",
        "usage_counters",
        "circuit_breakers",
        "route_logs",
        "project_registry",
    }


def test_three_metadata_do_not_overlap() -> None:
    config_tables = set(ConfigBase.metadata.tables)
    runtime_tables = set(RuntimeBase.metadata.tables)
    project_tables = set(ProjectBase.metadata.tables)
    assert not config_tables & runtime_tables
    assert not config_tables & project_tables
    assert not runtime_tables & project_tables


def test_api_key_only_exists_in_runtime() -> None:
    for table in ConfigBase.metadata.tables.values():
        assert "api_key" not in table.columns


def test_provider_holds_the_four_main_dimensions() -> None:
    """一个 provider = 名称 + base_url + api_key + 支持的模型列表。"""
    columns = set(Provider.__table__.columns.keys())
    assert {"code", "name", "base_url", "api_key"} <= columns
    assert "models" in Provider.__mapper__.relationships


def test_quota_tables_hang_off_provider_model() -> None:
    """额度、用量、熔断、Agent 绑定全部挂在 provider 下的模型记录上。"""
    for table in ("model_limits", "usage_counters", "circuit_breakers", "provider_agent_models"):
        columns = RuntimeBase.metadata.tables[table].columns
        assert "provider_model_id" in columns
        assert "model_id" not in columns


def test_provider_model_endpoint_and_driver_fallback() -> None:
    provider = Provider(
        code="ark_agent_plan",
        name="方舟 Agent Plan",
        base_url="https://ark.cn-beijing.volces.com/",
        api_key="sk-x",
        driver="openai_compat",
    )
    inherited = ProviderModel(provider=provider, model_id="glm-5.3")
    assert inherited.effective_driver == "openai_compat"
    assert inherited.endpoint() == "https://ark.cn-beijing.volces.com"

    overridden = ProviderModel(
        provider=provider,
        model_id="doubao-seedream-5.0-lite",
        driver="ark_image",
        api_path="/api/plan/v3/images/generations",
    )
    assert overridden.effective_driver == "ark_image"
    assert overridden.endpoint() == (
        "https://ark.cn-beijing.volces.com/api/plan/v3/images/generations"
    )


def test_route_log_keeps_flat_snapshot() -> None:
    """路由日志存字符串快照，删 provider 不影响历史可读性。"""
    columns = set(RuntimeBase.metadata.tables["route_logs"].columns.keys())
    assert {"provider_code", "model_id"} <= columns


def test_conversation_binds_provider_model_for_stickiness() -> None:
    """轮转粒度是会话：绑定关系必须落在会话上，才能跨轮复用同一前缀缓存。"""
    columns = Conversation.__table__.columns
    assert "bound_provider_model_id" in columns
    assert {"bound_at", "rebind_count", "rebind_reason"} <= set(columns.keys())


def test_conversation_binding_is_a_cross_database_reference() -> None:
    """会话在项目库、模型在全局库，所以只存裸 id，不得有外键。

    绑上了外键，项目目录拷到另一台机器就建不起库；悬空 id 的处理交给选路层（找不到
    候选就换绑），效果与 SET NULL 一致。
    """
    column = Conversation.__table__.c.bound_provider_model_id
    assert not column.foreign_keys
    assert column.nullable is True


def test_project_db_has_no_foreign_key_pointing_outside() -> None:
    """项目库里的外键只能指向项目库自己的表。"""
    for table in ProjectBase.metadata.tables.values():
        for fk in table.foreign_keys:
            assert fk.column.table.name in ProjectBase.metadata.tables
