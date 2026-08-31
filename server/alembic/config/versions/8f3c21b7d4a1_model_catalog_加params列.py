"""model_catalog 加 params 列

Revision ID: 8f3c21b7d4a1
Revises: 6014a40dcdd0
Create Date: 2026-09-01 10:12:44.118203
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "8f3c21b7d4a1"
down_revision: str | None = "6014a40dcdd0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 已有行先落成空对象，紧随其后的 atelier-seed 会按 seeds/ 覆写成真值
    with op.batch_alter_table("model_catalog", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("params", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )


def downgrade() -> None:
    with op.batch_alter_table("model_catalog", schema=None) as batch_op:
        batch_op.drop_column("params")
