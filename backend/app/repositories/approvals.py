from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentEventModel, ApprovalModel, ToolCallModel
from app.domain.execution import ApprovalStatus, ToolCallStatus


class ApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add_pending(self, approval_id: UUID, tool_call_id: UUID) -> None:
        self.session.add(ApprovalModel(
            id=approval_id, tool_call_id=tool_call_id, status=ApprovalStatus.PENDING.value,
        ))

    async def has_active(self, run_id: UUID) -> bool:
        statement = (
            select(ApprovalModel.id)
            .join(ToolCallModel, ApprovalModel.tool_call_id == ToolCallModel.id)
            .where(
                ToolCallModel.agent_run_id == run_id,
                ToolCallModel.status == ToolCallStatus.PROPOSED.value,
                ApprovalModel.status.in_((ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value)),
            )
            .limit(1)
        )
        return await self.session.scalar(statement) is not None

    @staticmethod
    def _owned_approval(approval_id: UUID, tool_call_id: UUID):
        return select(ApprovalModel).where(
            ApprovalModel.id == approval_id, ApprovalModel.tool_call_id == tool_call_id,
        )

    async def get(self, approval_id: UUID, tool_call_id: UUID) -> ApprovalModel | None:
        return await self.session.scalar(self._owned_approval(approval_id, tool_call_id))

    async def get_for_update(self, approval_id: UUID, tool_call_id: UUID) -> ApprovalModel | None:
        statement = self._owned_approval(approval_id, tool_call_id).with_for_update()
        return await self.session.scalar(statement.execution_options(populate_existing=True))

    async def event_payloads(self, run_id: UUID, approval_id: UUID, event_type: str) -> list[dict]:
        statement = select(AgentEventModel.payload).where(
            AgentEventModel.agent_run_id == run_id,
            AgentEventModel.event_type == event_type,
            AgentEventModel.payload["approval_id"].astext == str(approval_id),
        ).limit(2)
        return list((await self.session.scalars(statement)).all())
