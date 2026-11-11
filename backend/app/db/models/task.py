from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.execution import status_check
from app.domain.task import TaskStatus


class TaskModel(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("goal ~ '[^[:space:]]'", name="ck_tasks_goal_not_blank"),
        CheckConstraint(
            "cardinality(acceptance_criteria) BETWEEN 1 AND 10",
            name="ck_tasks_acceptance_criteria_count",
        ),
        status_check(TaskStatus, "ck_tasks_status"),
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_tasks_workspace_idempotency_key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    goal: Mapped[str] = mapped_column(String(500), nullable=False)
    acceptance_criteria: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=TaskStatus.QUEUED.value)
    idempotency_key: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
