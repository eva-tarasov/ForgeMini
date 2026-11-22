"""ADR-003 / C5: drop the M1 tables (tool_calls, approvals, artifacts) and the waiting_approval status.

Revision ID: c5a1f0d2e7b9
Revises: a7c3e91d4b20

Why these tables go: the approval layer (C3) and the LangGraph runtime (C4) were their only writers; ForgeMini
records tool use as agent_step events and keeps artifacts (patch, transcript) as files (FileArtifactStore).

Fail-closed on old data: a run or task still in 'waiting_approval' (an M1 run paused for an approval that can no
longer be given) makes the upgrade STOP with a message, instead of silently rewriting its status without an event.
Rows in the dropped tables are discarded by design (owner decision, ADR-003 Q2).
"""
from alembic import op
import sqlalchemy as sa

revision = "c5a1f0d2e7b9"
down_revision = "a7c3e91d4b20"
branch_labels = None
depends_on = None

_WITH_APPROVAL = "status IN ('queued', 'running', 'waiting_approval', 'verifying', 'succeeded', 'failed', 'cancelled')"
_WITHOUT_APPROVAL = "status IN ('queued', 'running', 'verifying', 'succeeded', 'failed', 'cancelled')"


def upgrade() -> None:
    bind = op.get_bind()
    stuck = {table: bind.scalar(sa.text(f"SELECT count(*) FROM {table} WHERE status = 'waiting_approval'"))
             for table in ("agent_runs", "tasks")}
    if any(stuck.values()):
        raise RuntimeError(
            f"refusing to migrate: rows in status 'waiting_approval' {stuck}. They are M1 runs paused for an approval "
            "that ForgeMini can no longer give. Recreate the local database, or move them to 'cancelled' yourself "
            "(UPDATE ... SET status = 'cancelled'), then run the upgrade again.")

    op.drop_index("ix_approvals_tool_call_id", table_name="approvals")
    op.drop_table("approvals")
    op.drop_index("ix_tool_calls_agent_run_id", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_index("ix_artifacts_agent_run_id", table_name="artifacts")
    op.drop_table("artifacts")

    for table in ("agent_runs", "tasks"):
        op.drop_constraint(f"ck_{table}_status", table, type_="check")
        op.create_check_constraint(f"ck_{table}_status", table, _WITHOUT_APPROVAL)


def downgrade() -> None:
    for table in ("agent_runs", "tasks"):
        op.drop_constraint(f"ck_{table}_status", table, type_="check")
        op.create_check_constraint(f"ck_{table}_status", table, _WITH_APPROVAL)

    # Recreated exactly as in ecb758eac83b (the tables come back empty).
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=100), nullable=False),
        sa.Column("storage_ref", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind ~ '[^[:space:]]'", name="ck_artifacts_kind_not_blank"),
        sa.CheckConstraint("storage_ref ~ '[^[:space:]]'", name="ck_artifacts_storage_ref_not_blank"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifacts_agent_run_id", "artifacts", ["agent_run_id"], unique=False)
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('proposed', 'running', 'succeeded', 'failed', 'denied')",
                           name="ck_tool_calls_status"),
        sa.CheckConstraint("tool_name ~ '[^[:space:]]'", name="ck_tool_calls_name_not_blank"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_calls_agent_run_id", "tool_calls", ["agent_run_id"], unique=False)
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_approvals_status"),
        sa.ForeignKeyConstraint(["tool_call_id"], ["tool_calls.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approvals_tool_call_id", "approvals", ["tool_call_id"], unique=False)
