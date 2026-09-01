"""seed studio director model binding

Revision ID: c3d71a8e4f20
Revises: e2b4c6d8f901
Create Date: 2026-09-01 20:00:00.000000

首次升级时为总管复制一个已经启用的文本 Agent 模型候选。若机器尚未配置任何可用文本
模型则保持为空，由设置页阻止发消息并提示用户绑定。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c3d71a8e4f20"
down_revision: str | None = "e2b4c6d8f901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO provider_agent_models (agent_code, provider_model_id, enabled, params)
        SELECT 'studio_director', pam.provider_model_id, 1, pam.params
        FROM provider_agent_models AS pam
        JOIN provider_models AS model ON model.id = pam.provider_model_id
        JOIN providers AS provider ON provider.code = model.provider_code
        WHERE pam.enabled = 1
          AND model.enabled = 1
          AND provider.enabled = 1
          AND model.capabilities LIKE '%"text"%'
          AND NOT EXISTS (
              SELECT 1 FROM provider_agent_models
              WHERE agent_code = 'studio_director'
          )
        ORDER BY provider.priority, model.sort_no, model.id, pam.id
        LIMIT 1
        """
    )


def downgrade() -> None:
    # 绑定可能已被用户修改；数据迁移回滚时保留现有配置，避免误删。
    pass
