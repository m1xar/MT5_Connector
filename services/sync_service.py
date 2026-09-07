from __future__ import annotations

import asyncio
import logging
import uuid

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
from utils.logging import bind_context, log_event, reset_context

from .terminal_affinity import ensure_assigned

logger = logging.getLogger(__name__)


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
        if dedupe:
            pending = self.pool.pending_for(account.account_id)
            if pending is not None:
                log_event(logger, "debug", "sync.request.deduped", account_id=account.account_id)
                run_id, future = pending
                return run_id or str(uuid.uuid4()), future

        sync_run_id = str(uuid.uuid4())
        terminal_path = await ensure_assigned(session, account, self.pool.terminal_paths)
        already_measured = frozenset(
            await PositionRepository(session).measured_external_ids(account.account_id)
        )
        future = self.pool.submit(
            account_id=account.account_id,
            login=account.login,
            password=account.password,
            server=account.server,
            terminal_path=terminal_path,
            priority=PRIORITY_HARD if kind in (SyncKind.hard, SyncKind.initial) else PRIORITY_SCHEDULED,
            kind=kind,
            sync_run_id=sync_run_id,
            connect_timeout_ms=connect_timeout_ms,
            already_measured=already_measured,
            max_retries=0 if kind == SyncKind.initial else None,
        )
        return sync_run_id, future

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
                log_event(logger, "error", "scheduler.tick.failed", error=str(exc))

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
            log_event(logger, "warning", "sync.persist.account_missing", account_id=result.account_id)
            return

        if not result.ok or result.payload is None:
            await account_repo.mark_error(
                account,
                result.error or "unknown error",
                initial=result.kind == SyncKind.initial,
                threshold=settings.account_error_threshold,
            )
            log_event(
                logger, "warning", "sync.persist.failed",
                kind=result.kind.value, error=result.error,
                consecutive_failures=account.consecutive_failures, status=account.status.value,
            )
            return

        payload = result.payload
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
