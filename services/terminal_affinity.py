from __future__ import annotations

import logging
from typing import Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import MT5Account
from repositories.account_repo import AccountRepository
from utils.logging import log_event

logger = logging.getLogger(__name__)


def pick_terminal(terminal_paths: Sequence[str], counts: Mapping[str, int]) -> str:
    return min(terminal_paths, key=lambda path: counts.get(path, 0))


def assigned_terminal(account: MT5Account, terminal_paths: Sequence[str]) -> str | None:
    return account.terminal_path if account.terminal_path in terminal_paths else None


async def ensure_assigned(session: AsyncSession, account: MT5Account, terminal_paths: Sequence[str]) -> str:
    path = assigned_terminal(account, terminal_paths)
    if path is not None:
        return path
    repo = AccountRepository(session)
    previous = account.terminal_path
    account.terminal_path = pick_terminal(terminal_paths, await repo.counts_by_terminal())
    await repo.update(account)
    await session.commit()
    log_event(
        logger, "info", "sync.terminal.assigned",
        account_id=account.account_id, terminal_path=account.terminal_path, previous=previous,
    )
    return account.terminal_path
