"""model_catalog 加预设列

Revision ID: 6014a40dcdd0
Revises: 93a010edb77a
Create Date: 2026-08-30 19:48:07.536319
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "6014a40dcdd0"
down_revision: str | None = "93a010edb77a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 已有行先落到 server_default 上，紧随其后的 atelier-seed 会按 seeds/ 覆写成真值
    with op.batch_alter_table("model_catalog", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("preset_code", sa.String(length=64), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("limit_kind", sa.String(length=16), nullable=False, server_default="tokens")
        )
        batch_op.add_column(
            sa.Column("default_period", sa.String(length=32), nullable=False, server_default="day")
        )


def downgrade() -> None:
    with op.batch_alter_table("model_catalog", schema=None) as batch_op:
        batch_op.drop_column("default_period")
        batch_op.drop_column("limit_kind")
        batch_op.drop_column("preset_code")
