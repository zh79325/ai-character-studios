"""character scoped project memory

Revision ID: 84547fef14be
Revises: a1c1ee363bc3
Create Date: 2026-08-30 16:00:02.687236

给 `project_memory` 加作用域：空串是项目级，填了角色 id 就只注入那个角色的会话。
已有的记忆全部归为项目级（`server_default=""`）：它们当时就是按项目级注入的，事后
猜它归哪个角色只会把人家已经生效的偏好藏起来。

唯一约束跟着改成 (content_hash, character_ref)：同一句话对不同角色各算一条。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "84547fef14be"
down_revision: str | None = "a1c1ee363bc3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project_memory", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("character_ref", sa.String(length=64), nullable=False, server_default="")
        )
        batch_op.drop_constraint(batch_op.f("uq_project_memory"), type_="unique")
        batch_op.create_unique_constraint("uq_project_memory", ["content_hash", "character_ref"])


def downgrade() -> None:
    with op.batch_alter_table("project_memory", schema=None) as batch_op:
        batch_op.drop_constraint("uq_project_memory", type_="unique")
        batch_op.create_unique_constraint(batch_op.f("uq_project_memory"), ["content_hash"])
        batch_op.drop_column("character_ref")
