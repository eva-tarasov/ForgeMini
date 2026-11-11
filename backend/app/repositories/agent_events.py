from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentEventModel
from app.domain.execution import AgentRunStatus


class AgentEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_status_changed(
        self, run_id: UUID, current: AgentRunStatus, target: AgentRunStatus
    ) -> AgentEventModel:
        """Caller must hold the parent AgentRun row lock until transaction end."""
        statement = select(func.coalesce(func.max(AgentEventModel.sequence), 0) + 1).where(
            AgentEventModel.agent_run_id == run_id
        )
        sequence = await self.session.scalar(statement)
        event = AgentEventModel(
            agent_run_id=run_id,
            sequence=sequence,
            event_type="agent_run_status_changed",
            payload={"from": current.value, "to": target.value},
        )
        self.session.add(event)
        return event
