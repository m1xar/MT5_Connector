from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from db.session import async_session_factory
from domain.enums import SyncKind
from domain.models import MT5Account
from pool.manager import PoolManager
from pool.protocol import PRIORITY_HARD, PRIORITY_SCHEDULED, SyncResult
from repositories.account_repo import AccountRepository
from repositories.position_repo import OpenPositionRepository, PositionRepository
from repositories.transaction_repo import TransactionRepository
from utils.config import settings
from utils.id import new_id
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
    ) -> tuple[str, "asyncio.Future[SyncResult]"]:
        """Queue a sync, or hand back the one already running for this account.

        Returns the correlation id every log line of this sync will carry. It
        is not stored anywhere: a sync is either in flight - and then the pool
        knows about it - or finished, and then its outcome is on the account
        itself.
        """
        if dedupe:
            pending = self.pool.pending_for(account.account_id)
            if pending is not None:
                log_event(
                    logger,
                    "debug",
                    "sync.request.deduped",
                    account_id=account.account_id,
                )
                run_id, future = pending
                return run_id or new_id(), future

        sync_run_id = new_id()

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
            kind=kind,
            sync_run_id=sync_run_id,
            connect_timeout_ms=connect_timeout_ms,
            already_measured=already_measured,
            server_offset_minutes=account.server_utc_offset_minutes,
            # One attempt, on the long timeout it was already given. Three
            # attempts turned a 120s budget into a 400s wait.
            max_retries=0 if kind == SyncKind.initial else None,
        )
        return sync_run_id, future

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
        account = await account_repo.get(result.account_id)

        if account is None:
            log_event(
                logger,
                "warning",
                "sync.persist.account_missing",
                account_id=result.account_id,
            )
            return

        if not result.ok or result.payload is None:
            await account_repo.mark_error(
                account,
                result.error or "unknown error",
                # A failed first sync is conclusive on its own, and the kind
                # travels with the result rather than being looked up.
                initial=result.kind == SyncKind.initial,
                threshold=settings.account_error_threshold,
            )
            log_event(
                logger,
                "warning",
                "sync.persist.failed",
                kind=result.kind.value,
                error=result.error,
                consecutive_failures=account.consecutive_failures,
                status=account.status.value,
            )
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
            server_utc_offset_minutes=payload.server_utc_offset_minutes,
        )
        log_event(
            logger,
            "info",
            "sync.persist.completed",
            kind=result.kind.value,
            positions=positions_count,
            open_positions=open_count,
            new_transactions=transactions_count,
            server_utc_offset_minutes=payload.server_utc_offset_minutes,
            duration_ms=result.duration_ms,
        )
