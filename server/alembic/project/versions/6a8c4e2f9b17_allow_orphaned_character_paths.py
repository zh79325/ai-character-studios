"""allow orphaned character directory paths

Revision ID: 6a8c4e2f9b17
Revises: 2287bb598963
Create Date: 2026-09-01 13:00:00.000000

角色身份改由 `.model.json` 中的随机 ID 决定。旧记录与新角色可以暂时指向同一个目录路径，
扫描再按 marker ID 把旧记录报告为缺失，因此 `dir_name` 不再唯一。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "6a8c4e2f9b17"
down_revision: str | None = "2287bb598963"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.drop_constraint("uq_character_dir", type_="unique")


def downgrade() -> None:
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_character_dir", ["dir_name"])
