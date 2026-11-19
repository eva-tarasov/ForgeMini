from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentRunModel
from app.domain.execution import AgentRunStatus


class AgentRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, run_id: UUID) -> AgentRunModel | None:
        statement = select(AgentRunModel).where(AgentRunModel.id == run_id)
        return await self.session.scalar(statement)

    async def list_for_task(self, task_id: UUID, *, limit: int) -> list[AgentRunModel]:
        statement = (
            select(AgentRunModel)
            .where(AgentRunModel.task_id == task_id)
            .order_by(AgentRunModel.attempt_number)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.scalars(statement)).all())

    async def add_initial(self, task_id: UUID) -> AgentRunModel:
        run = AgentRunModel(task_id=task_id, attempt_number=1, status=AgentRunStatus.QUEUED.value)
        self.session.add(run)
        await self.session.flush()
        return run

    async def get_for_update(self, run_id: UUID) -> AgentRunModel | None:
        # Use the locked database row even if this session loaded the run earlier.
        statement = (
            select(AgentRunModel)
            .where(AgentRunModel.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(statement)
