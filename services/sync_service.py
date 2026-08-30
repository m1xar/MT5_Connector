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
from utils.config import settings
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
        connect_timeout_ms: int | None = None,
    ) -> tuple[MT5SyncRun | None, "asyncio.Future[SyncResult]"]:
        # A deduped request rides on the sync already running, so recording a
        # second run would leave a row stuck at `queued` for ever - nothing
        # will ever finish it, because no second task exists.
        if dedupe:
            pending = self.pool.pending_for(account.account_id)
            if pending is not None:
                log_event(
                    logger,
                    "debug",
                    "sync.request.deduped",
                    account_id=account.account_id,
                )
                return None, pending

        run = await SyncRunRepository(session).create(account.account_id, kind)
        await session.commit()

        # Only the parent can see the database, so the list of positions that
        # already carry an excursion is gathered here and carried to the
        # worker with the task.
        already_measured = frozenset(
            await PositionRepository(session).measured_external_ids(account.account_id)
        )

        future = self.pool.submit(
            account_id=account.account_id,
            login=account.login,
            password=account.password,
            server=account.server,
            priority=(
                PRIORITY_HARD
                if kind in (SyncKind.hard, SyncKind.initial)
                else PRIORITY_SCHEDULED
            ),
            dedupe=dedupe,
            sync_run_id=run.sync_run_id,
            connect_timeout_ms=connect_timeout_ms,
            already_measured=already_measured,
            # One attempt, on the long timeout it was already given. Three
            # attempts turned a 120s budget into a 400s wait.
            max_retries=0 if kind == SyncKind.initial else None,
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
            await account_repo.mark_error(
                account,
                result.error or "unknown error",
                initial=run is not None and run.kind == SyncKind.initial,
                threshold=settings.account_error_threshold,
            )
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
