from __future__ import annotations

import asyncio
from typing import Any

from db.session import async_session_factory
from domain.enums import SyncKind
from repositories.account_repo import AccountRepository
from services.sync_service import SyncService
from utils.config import settings
from utils.logging import get_logger, log_event

logger = get_logger(__name__)

_bg_tasks: set[asyncio.Task[Any]] = set()


def track_bg_task(task: asyncio.Task[Any]) -> None:
    _bg_tasks.add(task)
    task.add_done_callback(lambda finished: _bg_tasks.discard(finished))


async def cancel_background_tasks() -> None:
    for task in list(_bg_tasks):
        task.cancel()
    await asyncio.gather(*list(_bg_tasks), return_exceptions=True)


async def scheduler_loop(sync_service: SyncService) -> None:
    while True:
        try:
            await asyncio.sleep(settings.sync_scheduler_tick_seconds)
            await _enqueue_due_accounts(sync_service)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(logger, "error", "scheduler.tick.failed", error=str(exc))


async def _enqueue_due_accounts(sync_service: SyncService) -> None:
    async with async_session_factory() as session:
        accounts = await AccountRepository(session).due_for_sync(settings.sync_interval_minutes)
        if not accounts:
            return
        for account in accounts:
            await sync_service.request(session, account, SyncKind.scheduled, dedupe=True)
    log_event(logger, "info", "scheduler.tick.enqueued", accounts=len(accounts))
