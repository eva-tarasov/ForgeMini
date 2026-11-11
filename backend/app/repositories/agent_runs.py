from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentRunModel


class AgentRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_for_update(self, run_id: UUID) -> AgentRunModel | None:
        statement = select(AgentRunModel).where(AgentRunModel.id == run_id).with_for_update()
        return await self.session.scalar(statement)
