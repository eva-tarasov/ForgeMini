from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentRunModel


class AgentRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, run_id: UUID) -> AgentRunModel | None:
        statement = select(AgentRunModel).where(AgentRunModel.id == run_id)
        return await self.session.scalar(statement)

    async def get_for_update(self, run_id: UUID) -> AgentRunModel | None:
        # Use the locked database row even if this session loaded the run earlier.
        statement = (
            select(AgentRunModel)
            .where(AgentRunModel.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(statement)
