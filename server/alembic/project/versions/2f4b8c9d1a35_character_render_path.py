"""character render path

Revision ID: 2f4b8c9d1a35
Revises: 84547fef14be
Create Date: 2026-08-30 20:10:00.000000

给 `characters` 加 `render_path`：门禁 2 采用的那张渲染图。

候选全在 `generations` 里，但「人采用的是哪一张」是角色自己的状态，后面四视图、模型每一步
都要拿它当参考图。每次都去台账里按 `is_final` 筛一遍，等于把一个确定的结论反复推导一次。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2f4b8c9d1a35"
down_revision: str | None = "84547fef14be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.add_column(sa.Column("render_path", sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.drop_column("render_path")
