from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from db.session import async_session_factory
from domain.enums import SyncKind, SyncStatus
from domain.models import MT5Account, MT5SyncRun
from pool.manager import PoolManager
from pool.protocol import PRIORITY_HARD, PRIORITY_SCHEDULED, SyncResult
from repositories.account_repo import AccountRepository
from repositories.position_repo import OpenPositionRepository, PositionRepository
from repositories.sync_run_repo import SyncRunRepository
from repositories.transaction_repo import TransactionRepository
from utils.logging import get_logger, log_event, reset_context, set_sync_context

logger = get_logger(__name__)


class SyncService:
    def __init__(self, pool: PoolManager) -> None:
        self.pool = pool

    async def request(
        self,
        session: AsyncSession,
        account: MT5Account,
        kind: SyncKind,
        dedupe: bool = False,
    ) -> tuple[MT5SyncRun, "asyncio.Future[SyncResult]"]:
        run = await SyncRunRepository(session).create(account.account_id, kind)
        await session.commit()

        future = self.pool.submit(
            account_id=account.account_id,
            login=account.login,
            password=account.password,
            server=account.server,
            priority=PRIORITY_HARD if kind == SyncKind.hard else PRIORITY_SCHEDULED,
            dedupe=dedupe,
            sync_run_id=run.sync_run_id,
        )
        return run, future

    async def persist(self, result: SyncResult) -> None:
        tokens = set_sync_context(
            sync_run_id=result.sync_run_id,
            worker_id=result.worker_id,
            account_id=result.account_id,
        )
        try:
            async with async_session_factory() as session:
                try:
                    await self._persist(session, result)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
        finally:
            reset_context(tokens)

    async def _persist(self, session: AsyncSession, result: SyncResult) -> None:
        account_repo = AccountRepository(session)
        run_repo = SyncRunRepository(session)

        account = await account_repo.get(result.account_id)
        run = await run_repo.get(result.sync_run_id) if result.sync_run_id else None

        if account is None:
            log_event(logger, "warning", "sync.persist.account_missing", account_id=result.account_id)
            if run is not None:
                await run_repo.finish(
                    run, status=SyncStatus.failed, worker_id=result.worker_id, error="account deleted"
                )
            return

        if not result.ok or result.payload is None:
            await account_repo.mark_error(account, result.error or "unknown error")
            if run is not None:
                await run_repo.finish(
                    run,
                    status=SyncStatus.failed,
                    worker_id=result.worker_id,
                    duration_ms=result.duration_ms,
                    error=result.error,
                )
            log_event(logger, "warning", "sync.persist.failed", error=result.error)
            return

        payload = result.payload
        positions_count = await PositionRepository(session).upsert_many(
            account.account_id, payload.positions
        )
        open_count = await OpenPositionRepository(session).replace_all(
            account.account_id, payload.open_positions
        )
        transactions_count = await TransactionRepository(session).upsert_many(
            account.account_id, payload.transactions
        )
        await account_repo.mark_synced(
            account,
            balance=payload.account_info.balance,
            equity=payload.equity,
            leverage=payload.account_info.leverage,
            currency=payload.account_info.currency,
        )
        if run is not None:
            await run_repo.finish(
                run,
                status=SyncStatus.completed,
                worker_id=result.worker_id,
                duration_ms=result.duration_ms,
                positions_count=positions_count,
                open_positions_count=open_count,
                transactions_count=transactions_count,
            )
        log_event(
            logger,
            "info",
            "sync.persist.completed",
            positions=positions_count,
            open_positions=open_count,
            new_transactions=transactions_count,
            duration_ms=result.duration_ms,
        )
