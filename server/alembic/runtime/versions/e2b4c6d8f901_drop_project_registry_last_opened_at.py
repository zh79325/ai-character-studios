"""drop project registry last_opened_at

项目注册表只保存本机索引信息，不记录访问状态。项目上下文由每次请求 URL 中的
project_code 唯一确定。

Revision ID: e2b4c6d8f901
Revises: 5c1e0a9d84f2
Create Date: 2026-09-01 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e2b4c6d8f901"
down_revision: str | None = "5c1e0a9d84f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project_registry", schema=None) as batch_op:
        batch_op.drop_column("last_opened_at")


def downgrade() -> None:
    with op.batch_alter_table("project_registry", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_opened_at", sa.DateTime(), nullable=True))
