from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from db.session import async_session_factory
from domain.enums import SyncKind
from domain.models import MT5Account
from pool.manager import PoolManager
from pool.protocol import SyncResult
from repositories.account_repo import AccountRepository
from repositories.position_repo import OpenPositionRepository, PositionRepository
from repositories.transaction_repo import TransactionRepository
from utils.config import settings
from utils.logging import bind_context, log_event, reset_context

from .account_service import AccountService
from .terminal_affinity import ensure_assigned

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(self, pool: PoolManager) -> None:
        self.pool = pool
        self._submit_lock = asyncio.Lock()

    async def request(
        self,
        session: AsyncSession,
        account: MT5Account,
        kind: SyncKind,
        dedupe: bool = False,
        connect_timeout_ms: int | None = None,
    ) -> asyncio.Future[SyncResult]:
        async with self._submit_lock:
            if dedupe:
                pending = self.pool.pending_for(account.account_id)
                if pending is not None:
                    log_event(logger, "debug", "sync.request.deduped", account_id=account.account_id)
                    return pending

            terminal_path = await ensure_assigned(session, account, self.pool.terminal_paths)
            already_measured = frozenset(
                await PositionRepository(session).measured_external_ids(account.account_id)
            )
            return self.pool.submit(
                account_id=account.account_id,
                login=account.login,
                password=account.password,
                server=account.server,
                terminal_path=terminal_path,
                kind=kind,
                sync_run_id=str(uuid.uuid4()),
                connect_timeout_ms=connect_timeout_ms,
                already_measured=already_measured,
            )

    async def run_scheduler(self) -> None:
        while True:
            try:
                await asyncio.sleep(settings.sync_scheduler_tick_seconds)
                async with async_session_factory() as session:
                    accounts = await AccountRepository(session).due_for_sync(settings.sync_interval_minutes)
                    for account in accounts:
                        await self.request(session, account, SyncKind.scheduled, dedupe=True)
                if accounts:
                    log_event(logger, "info", "scheduler.tick.enqueued", accounts=len(accounts))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(logger, "error", "scheduler.tick.failed", error=f"{type(exc).__name__}: {exc}")

    async def persist(self, result: SyncResult) -> None:
        tokens = bind_context(sync_run_id=result.sync_run_id, worker_id=result.worker_id, account_id=result.account_id)
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
            log_event(logger, "warning", "sync.persist.account_missing")
            return

        if not result.ok or result.payload is None:
            await account_repo.mark_error(
                account,
                result.error or "unknown error",
                initial=result.kind is SyncKind.initial,
                threshold=settings.account_error_threshold,
            )
            log_event(
                logger, "warning", "sync.persist.failed",
                kind=result.kind.value, error=result.error,
                consecutive_failures=account.consecutive_failures, status=account.status.value,
            )
            return

        payload = result.payload
        if result.history_withheld:
            await self._persist_withheld(session, account_repo, account, result)
            return

        positions_count = await PositionRepository(session).upsert_many(account.account_id, payload.positions)
        open_count = await OpenPositionRepository(session).replace_all(account.account_id, payload.open_positions)
        transactions_count = await TransactionRepository(session).upsert_many(account.account_id, payload.transactions)
        await account_repo.mark_synced(
            account,
            balance=payload.account_info.balance,
            equity=payload.equity,
            leverage=payload.account_info.leverage,
            currency=payload.account_info.currency,
            server_clock=payload.server_clock,
        )
        log_event(
            logger, "info", "sync.persist.completed",
            kind=result.kind.value, positions=positions_count, open_positions=open_count,
            new_transactions=transactions_count, server_clock_switches=max(len(payload.server_clock) - 1, 0),
            duration_ms=result.duration_ms,
        )

    async def _persist_withheld(
        self, session: AsyncSession, account_repo: AccountRepository, account: MT5Account, result: SyncResult
    ) -> None:
        payload = result.payload
        if result.kind is SyncKind.initial:
            await AccountService(session).delete(account)
            log_event(logger, "warning", "sync.persist.withheld.rejected", login=account.login, server=account.server)
            return
        open_count = await OpenPositionRepository(session).replace_all(account.account_id, payload.open_positions)
        await account_repo.mark_withheld(
            account,
            balance=payload.account_info.balance,
            equity=payload.equity,
            leverage=payload.account_info.leverage,
            currency=payload.account_info.currency,
            server_clock=payload.server_clock,
            pause_minutes=settings.history_withheld_pause_minutes,
        )
        log_event(
            logger, "warning", "sync.persist.withheld",
            kind=result.kind.value, open_positions=open_count,
            until=account.history_withheld_until.isoformat(), duration_ms=result.duration_ms,
        )
