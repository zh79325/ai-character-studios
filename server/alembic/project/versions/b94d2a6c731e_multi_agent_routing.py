"""add multi-agent conversation routing state

Revision ID: b94d2a6c731e
Revises: 6a8c4e2f9b17
Create Date: 2026-09-01 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b94d2a6c731e"
down_revision: str | None = "6a8c4e2f9b17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("focus_agent_code", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("focus_started_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("focus_reason", sa.String(length=255), nullable=True))

    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "recipient_agent_code", sa.String(length=64), nullable=False, server_default=""
            )
        )

    op.create_table(
        "conversation_agent_bindings",
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("agent_code", sa.String(length=64), nullable=False),
        sa.Column("bound_provider_model_id", sa.Integer(), nullable=True),
        sa.Column("bound_provider_label", sa.String(length=255), nullable=False),
        sa.Column("bound_at", sa.DateTime(), nullable=True),
        sa.Column("rebind_count", sa.Integer(), nullable=False),
        sa.Column("rebind_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "agent_code", name="uq_conversation_agent_binding"),
    )
    with op.batch_alter_table("conversation_agent_bindings", schema=None) as batch_op:
        batch_op.create_index(
            "ix_conversation_agent_bindings_conv", ["conversation_id"], unique=False
        )

    op.create_table(
        "agent_handoffs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("turn_no", sa.Integer(), nullable=False),
        sa.Column("from_agent_code", sa.String(length=64), nullable=False),
        sa.Column("to_agent_code", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("agent_handoffs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_agent_handoffs_conv", ["conversation_id", "created_at"], unique=False
        )

    # 先按旧主 Agent 补齐消息与模型绑定，再把会话主 Agent 统一迁为总管。
    op.execute(
        """
        UPDATE messages
        SET agent_code = (
            SELECT conversations.agent_code FROM conversations
            WHERE conversations.id = messages.conversation_id
        )
        WHERE role = 'assistant' AND agent_code = ''
        """
    )
    op.execute(
        """
        UPDATE messages
        SET agent_code = 'user'
        WHERE role = 'user'
        """
    )
    op.execute(
        """
        UPDATE messages
        SET recipient_agent_code = (
            SELECT conversations.agent_code FROM conversations
            WHERE conversations.id = messages.conversation_id
        )
        WHERE role = 'user' AND recipient_agent_code = ''
        """
    )
    op.execute(
        """
        INSERT INTO conversation_agent_bindings (
            id, conversation_id, agent_code, bound_provider_model_id, bound_provider_label,
            bound_at, rebind_count, rebind_reason, created_at, updated_at
        )
        SELECT id || ':' || agent_code, id, agent_code, bound_provider_model_id,
               bound_provider_label, bound_at, rebind_count, rebind_reason, created_at, updated_at
        FROM conversations
        WHERE bound_provider_model_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE conversations
        SET focus_agent_code = CASE
                WHEN agent_code = 'studio_director' THEN NULL
                ELSE agent_code
            END,
            focus_started_at = CASE
                WHEN agent_code = 'studio_director' THEN NULL
                ELSE updated_at
            END,
            focus_reason = CASE
                WHEN agent_code = 'studio_director' THEN NULL
                ELSE '升级前主 Agent'
            END,
            agent_code = 'studio_director'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE conversations
        SET agent_code = COALESCE(focus_agent_code, 'studio_director')
        """
    )
    with op.batch_alter_table("agent_handoffs", schema=None) as batch_op:
        batch_op.drop_index("ix_agent_handoffs_conv")
    op.drop_table("agent_handoffs")
    with op.batch_alter_table("conversation_agent_bindings", schema=None) as batch_op:
        batch_op.drop_index("ix_conversation_agent_bindings_conv")
    op.drop_table("conversation_agent_bindings")
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_column("recipient_agent_code")
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_column("focus_reason")
        batch_op.drop_column("focus_started_at")
        batch_op.drop_column("focus_agent_code")
