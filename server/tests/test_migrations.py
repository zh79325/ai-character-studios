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

from atelier.db.migrate import downgrade_project, upgrade_project
from atelier.db.project_models import ProjectBase
from atelier.db.session import dispose_project_engine, project_engine


def table_names(db_path: Path) -> set[str]:
    engine = project_engine(db_path)
    try:
        return set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        dispose_project_engine(db_path)


def test_runtime_migration_drops_project_access_state(tmp_path: Path) -> None:
    """升级现有 runtime 库时，注册表不再保留最近打开时间。"""
    db_path = tmp_path / "runtime.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic/runtime/versions/e2b4c6d8f901_drop_project_registry_last_opened_at.py"
    )
    spec = spec_from_file_location("drop_project_registry_last_opened_at", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

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
