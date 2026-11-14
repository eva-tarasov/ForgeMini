"""Store preliminary AgentRun results as JSONB.

Revision ID: 1d972cc34fbe
Revises: 496f3a2c5b81
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "1d972cc34fbe"
down_revision = "496f3a2c5b81"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("final_result", JSONB(none_as_null=True), nullable=True))
    op.create_check_constraint(
        "ck_agent_runs_final_result_object",
        "agent_runs",
        "final_result IS NULL OR jsonb_typeof(final_result) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_runs_final_result_object", "agent_runs", type_="check")
    op.drop_column("agent_runs", "final_result")
