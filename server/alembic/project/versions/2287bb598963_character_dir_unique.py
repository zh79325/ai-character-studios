"""character uniqueness by dir_name

Revision ID: 2287bb598963
Revises: d7f084de8123
Create Date: 2026-08-31 15:20:00.000000

角色分组落地后，同一个角色名在不同分组（不同父目录）下可以同时存在，重名判定改看相对
路径。所以身份约束从「全局唯一的 name」换成「全局唯一的 dir_name」。存量项目库打开时
自动升到这一版；旧库若有跨目录重名的角色行，这一版建约束会失败，属于该先扫描对账的数据
问题，不在迁移里静默丢行。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "2287bb598963"
down_revision: str | None = "d7f084de8123"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.drop_constraint("uq_character_name", type_="unique")
        batch_op.create_unique_constraint("uq_character_dir", ["dir_name"])


def downgrade() -> None:
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.drop_constraint("uq_character_dir", type_="unique")
        batch_op.create_unique_constraint("uq_character_name", ["name"])
