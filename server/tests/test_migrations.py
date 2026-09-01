"""项目库迁移真的能建出声明的那套表。

别处的用例为了跑得快都用 `create_all` 造项目库（见 conftest 的 `projects_root`），所以「迁移
与模型是否一致」只在这里盯。它同时也是「项目换机器、换版本后第一次打开会被补齐」这条承诺
的验证点：那条路径走的就是 `upgrade_project`。

全局两库不在这里测：它们的 alembic env 从 settings 拿固定路径，跑一遍会动到 `db/` 下的真库。
"""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from atelier.db.migrate import downgrade_project, upgrade_project
from atelier.db.project_models import ProjectBase
from atelier.db.runtime_models import Provider, ProviderAgentModel, ProviderModel, RuntimeBase
from atelier.db.session import dispose_project_engine, project_engine


def table_names(db_path: Path) -> set[str]:
    engine = project_engine(db_path)
    try:
        return set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        dispose_project_engine(db_path)


def load_migration(name: str, path: Path):
    spec = spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_runtime_migration_drops_project_access_state(tmp_path: Path) -> None:
    """升级现有 runtime 库时，注册表不再保留最近打开时间。"""
    db_path = tmp_path / "runtime.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic/runtime/versions/e2b4c6d8f901_drop_project_registry_last_opened_at.py"
    )
    migration = load_migration("drop_project_registry_last_opened_at", migration_path)

    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE project_registry ("
                "code VARCHAR(64) PRIMARY KEY, name VARCHAR(128) NOT NULL, "
                "dir_path VARCHAR(1024) NOT NULL, managed BOOLEAN NOT NULL, "
                "missing BOOLEAN NOT NULL, last_opened_at DATETIME, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

    assert "last_opened_at" not in {
        column["name"] for column in inspect(engine).get_columns("project_registry")
    }


def test_runtime_migration_seeds_director_from_enabled_text_binding(tmp_path: Path) -> None:
    """首次升级复用现有文本候选，但不覆盖已经存在的总管绑定。"""
    db_path = tmp_path / "runtime.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    RuntimeBase.metadata.create_all(engine)
    with Session(engine) as session:
        provider = Provider(code="p", name="P", base_url="https://example.invalid", api_key="")
        text_model = ProviderModel(
            provider=provider,
            model_id="text-model",
            capabilities=["text"],
            sort_no=0,
        )
        session.add_all(
            [
                provider,
                ProviderAgentModel(
                    agent_code="spec_writer",
                    provider_model=text_model,
                    params={"temperature": 0.2},
                ),
            ]
        )
        session.commit()

    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic/runtime/versions/c3d71a8e4f20_seed_studio_director_binding.py"
    )
    migration = load_migration("seed_studio_director_binding", migration_path)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        rows = connection.execute(
            text(
                "SELECT provider_model_id, params FROM provider_agent_models "
                "WHERE agent_code = 'studio_director'"
            )
        ).all()

    assert len(rows) == 1
    assert rows[0].provider_model_id == 1
    assert '"temperature": 0.2' in rows[0].params


def test_runtime_migration_leaves_director_unbound_without_text_model(tmp_path: Path) -> None:
    """没有文本模型时保持未绑定，让设置页给出明确提示。"""
    db_path = tmp_path / "runtime.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    RuntimeBase.metadata.create_all(engine)
    with Session(engine) as session:
        provider = Provider(code="p", name="P", base_url="https://example.invalid", api_key="")
        image_model = ProviderModel(
            provider=provider,
            model_id="image-model",
            capabilities=["t2i"],
            sort_no=0,
        )
        session.add_all(
            [
                provider,
                ProviderAgentModel(agent_code="image_t2i", provider_model=image_model),
            ]
        )
        session.commit()

    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic/runtime/versions/c3d71a8e4f20_seed_studio_director_binding.py"
    )
    migration = load_migration("seed_studio_director_without_text", migration_path)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        count = connection.scalar(
            text("SELECT count(*) FROM provider_agent_models WHERE agent_code = 'studio_director'")
        )

    assert count == 0


def test_project_routing_migration_preserves_legacy_actor_and_binding(tmp_path: Path) -> None:
    """旧主 Agent 迁入焦点与独立绑定，旧消息补齐真实 sender/recipient。"""
    db_path = tmp_path / "project.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE conversations ("
                "id VARCHAR(64) PRIMARY KEY, target_kind VARCHAR(32) NOT NULL, "
                "target_ref VARCHAR(128), agent_code VARCHAR(64) NOT NULL, "
                "title VARCHAR(255) NOT NULL, status VARCHAR(16) NOT NULL, "
                "bound_provider_model_id INTEGER, bound_provider_label VARCHAR(255) NOT NULL, "
                "bound_at DATETIME, rebind_count INTEGER NOT NULL, rebind_reason VARCHAR(255), "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id VARCHAR(64) NOT NULL, "
                "turn_no INTEGER NOT NULL, role VARCHAR(16) NOT NULL, content TEXT NOT NULL, "
                "token_count INTEGER NOT NULL, folded BOOLEAN NOT NULL, "
                "status VARCHAR(16) NOT NULL, agent_code VARCHAR(64) NOT NULL, "
                "attachments JSON NOT NULL, created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO conversations VALUES ("
                "'legacy', 'character', 'c-1', 'spec_writer', '旧会话', 'active', "
                "7, 'provider/model', '2026-01-01', 2, 'old', '2026-01-01', '2026-01-02')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO messages (conversation_id, turn_no, role, content, token_count, "
                "folded, status, agent_code, attachments, created_at) VALUES "
                "('legacy', 1, 'user', '需求', 1, 0, 'done', '', '[]', '2026-01-01'), "
                "('legacy', 2, 'assistant', '方案', 1, 0, 'done', '', '[]', '2026-01-01')"
            )
        )

        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic/project/versions/b94d2a6c731e_multi_agent_routing.py"
        )
        migration = load_migration("multi_agent_routing", migration_path)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        conversation = connection.execute(
            text(
                "SELECT agent_code, focus_agent_code, focus_reason "
                "FROM conversations WHERE id = 'legacy'"
            )
        ).one()
        messages = connection.execute(
            text("SELECT role, agent_code, recipient_agent_code FROM messages ORDER BY turn_no")
        ).all()
        binding = connection.execute(
            text(
                "SELECT agent_code, bound_provider_model_id, bound_provider_label "
                "FROM conversation_agent_bindings WHERE conversation_id = 'legacy'"
            )
        ).one()

    assert tuple(conversation) == ("studio_director", "spec_writer", "升级前主 Agent")
    assert [tuple(row) for row in messages] == [
        ("user", "user", "spec_writer"),
        ("assistant", "spec_writer", ""),
    ]
    assert tuple(binding) == ("spec_writer", 7, "provider/model")


def test_migration_builds_exactly_the_declared_schema(tmp_path: Path) -> None:
    """表名与列名逐张核对：模型加了字段却忘了写迁移，就在这里露出来。"""
    db_path = tmp_path / "某个项目" / ".atelier" / "project.db"

    upgrade_project(db_path)

    engine = project_engine(db_path)
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) - {"alembic_version"} == set(
            ProjectBase.metadata.tables
        )
        for name, table in ProjectBase.metadata.tables.items():
            assert {column["name"] for column in inspector.get_columns(name)} == {
                column.name for column in table.columns
            }, f"{name} 的列与模型不一致"
    finally:
        dispose_project_engine(db_path)


def test_migration_carries_the_declared_indexes(tmp_path: Path) -> None:
    """索引是查询性能的一部分，漏建了不会报错，只会某天变慢。"""
    db_path = tmp_path / "p" / ".atelier" / "project.db"

    upgrade_project(db_path)

    engine = project_engine(db_path)
    try:
        inspector = inspect(engine)
        for name, table in ProjectBase.metadata.tables.items():
            declared = {index.name for index in table.indexes}
            actual = {index["name"] for index in inspector.get_indexes(name)}
            assert declared <= actual, f"{name} 缺索引：{declared - actual}"
    finally:
        dispose_project_engine(db_path)


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    """每次打开项目都会调它，所以已经在 head 时必须什么都不做。"""
    db_path = tmp_path / "p" / ".atelier" / "project.db"

    upgrade_project(db_path)
    before = table_names(db_path)
    upgrade_project(db_path)

    assert table_names(db_path) == before


def test_downgrade_really_undoes_it(tmp_path: Path) -> None:
    """回滚写成空壳的迁移在出事那天才会被发现，所以现在就跑一遍。"""
    db_path = tmp_path / "p" / ".atelier" / "project.db"
    upgrade_project(db_path)

    downgrade_project(db_path, "base")
    assert table_names(db_path) == set()

    upgrade_project(db_path)
    assert table_names(db_path) == set(ProjectBase.metadata.tables)


def test_upgrade_creates_the_data_dir_if_needed(tmp_path: Path) -> None:
    """导入一个还没有 `.atelier/` 的项目目录（比如别人只拷了素材）也得能开起来。"""
    db_path = tmp_path / "只有素材的项目" / ".atelier" / "project.db"
    assert not db_path.parent.exists()

    upgrade_project(db_path)

    assert db_path.is_file()
