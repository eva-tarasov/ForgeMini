from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentEventModel, AgentRunModel
from app.domain.execution import AgentRunStatus
from app.services.agent_failure import AGENT_EXECUTION_FAILED_EVENT


class AgentEventParentNotFoundError(LookupError):
    pass


class AgentEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_execution_failure_events(self, run_id: UUID) -> list[AgentEventModel]:
        statement = (
            select(AgentEventModel)
            .where(AgentEventModel.agent_run_id == run_id, AgentEventModel.event_type == AGENT_EXECUTION_FAILED_EVENT)
            .order_by(AgentEventModel.sequence)
            .limit(2)
        )
        return list((await self.session.scalars(statement)).all())

    async def add_status_changed(
        self, run_id: UUID, current: AgentRunStatus, target: AgentRunStatus
    ) -> AgentEventModel:
        return await self.append(
            run_id,
            event_type="agent_run_status_changed",
            payload={"from": current.value, "to": target.value},
        )

    async def append(self, run_id: UUID, *, event_type: str, payload: dict) -> AgentEventModel:
        """Stage an event for an existing or already flushed run at READ COMMITTED.

        Use a short single-run transaction. The flush covers the current unit of
        work, and the caller owns commit/rollback and thus the lock lifetime.
        """
        if not isinstance(event_type, str) or not event_type.strip() or len(event_type) > 100:
            raise ValueError("invalid_event_type")
        if not isinstance(payload, dict):
            raise TypeError("invalid_event_payload")

        lock_statement = select(AgentRunModel.id).where(AgentRunModel.id == run_id).with_for_update()
        with self.session.no_autoflush:
            parent_id = await self.session.scalar(lock_statement)
        if parent_id is None:
            raise AgentEventParentNotFoundError("agent_run_not_found")

        await self.session.flush()
        # This separate statement sees events committed while the lock query waited.
        sequence_statement = select(func.coalesce(func.max(AgentEventModel.sequence), 0) + 1).where(
            AgentEventModel.agent_run_id == run_id
        )
        sequence = await self.session.scalar(sequence_statement)
        event = AgentEventModel(
            agent_run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
        )
        self.session.add(event)
        return event
