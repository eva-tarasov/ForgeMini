from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ToolCallModel
from app.domain.execution import ToolCallStatus


class ToolCallRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add_proposed(self, run_id: UUID, tool_call_id: UUID, tool_name: str) -> None:
        self.session.add(ToolCallModel(
            id=tool_call_id,
            agent_run_id=run_id,
            tool_name=tool_name,
            status=ToolCallStatus.PROPOSED.value,
        ))

    async def get(self, run_id: UUID, tool_call_id: UUID) -> ToolCallModel | None:
        return await self.session.scalar(select(ToolCallModel).where(
            ToolCallModel.agent_run_id == run_id, ToolCallModel.id == tool_call_id,
        ))

    async def get_for_update(self, run_id: UUID, tool_call_id: UUID) -> ToolCallModel | None:
        statement = (
            select(ToolCallModel)
            .where(ToolCallModel.agent_run_id == run_id, ToolCallModel.id == tool_call_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(statement)
