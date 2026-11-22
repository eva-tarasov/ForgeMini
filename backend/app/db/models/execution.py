from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.execution import AgentRunStatus


def status_check(enum: type[Enum], name: str) -> CheckConstraint:
    values = ", ".join("'" + member.value.replace("'", "''") + "'" for member in enum)
    return CheckConstraint(f"status IN ({values})", name=name)


class AgentRunModel(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_agent_runs_task_attempt"),
        CheckConstraint("attempt_number >= 1", name="ck_agent_runs_attempt_positive"),
        status_check(AgentRunStatus, "ck_agent_runs_status"),
        CheckConstraint(
            "final_result IS NULL OR jsonb_typeof(final_result) = 'object'",
            name="ck_agent_runs_final_result_object",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    final_result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AgentEventModel(Base):
    __tablename__ = "agent_events"
    __table_args__ = (
        UniqueConstraint("agent_run_id", "sequence", name="uq_agent_events_run_sequence"),
        CheckConstraint("sequence >= 1", name="ck_agent_events_sequence_positive"),
        CheckConstraint("event_type ~ '[^[:space:]]'", name="ck_agent_events_type_not_blank"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    agent_run_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

