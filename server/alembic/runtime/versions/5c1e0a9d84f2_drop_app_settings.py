"""drop app_settings

「打开的是哪个项目」曾经存在 app_settings 里，现在只活在进程内存里：它是会话事实而不是
要攻下来的数据，存进库里的后果是用户下次启动就看见一个自己没点过的项目被当成已打开。
这张表除此之外没别的键，所以整张删掉。

Revision ID: 5c1e0a9d84f2
Revises: 37048de48438
Create Date: 2026-08-30 23:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '5c1e0a9d84f2'
down_revision: str | None = '37048de48438'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table('app_settings')


def downgrade() -> None:
    op.create_table('app_settings',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
