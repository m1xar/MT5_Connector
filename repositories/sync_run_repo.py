from __future__ import annotations

from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from domain.enums import SyncKind, SyncStatus
from domain.models import MT5SyncRun, utc_now


class SyncRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, account_id: str, kind: SyncKind) -> MT5SyncRun:
        run = MT5SyncRun(account_id=account_id, kind=kind, status=SyncStatus.queued)
        self.session.add(run)
        await self.session.flush()
        await self.session.refresh(run)
        return run

    async def get(self, sync_run_id: str) -> Optional[MT5SyncRun]:
        result = await self.session.execute(
            select(MT5SyncRun).where(MT5SyncRun.sync_run_id == sync_run_id)
        )
        return result.scalars().first()

    async def finish(
        self,
        run: MT5SyncRun,
        *,
        status: SyncStatus,
        worker_id: str | None = None,
        duration_ms: int | None = None,
        positions_count: int | None = None,
        open_positions_count: int | None = None,
        transactions_count: int | None = None,
        error: str | None = None,
    ) -> MT5SyncRun:
        run.status = status
        run.worker_id = worker_id
        run.duration_ms = duration_ms
        run.positions_count = positions_count
        run.open_positions_count = open_positions_count
        run.transactions_count = transactions_count
        run.error = error
        run.finished_at = utc_now()
        self.session.add(run)
        await self.session.flush()
        await self.session.refresh(run)
        return run

    async def list_for_account(self, account_id: str, limit: int = 20) -> List[MT5SyncRun]:
        result = await self.session.execute(
            select(MT5SyncRun)
            .where(MT5SyncRun.account_id == account_id)
            .order_by(MT5SyncRun.started_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
