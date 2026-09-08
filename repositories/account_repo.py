from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from domain.enums import AccountStatus
from domain.models import MT5Account, utc_now


def _active(statement):
    return statement.where(MT5Account.enabled.is_(True)).where(MT5Account.status != AccountStatus.error_connection)


_DUE_BATCH = 200


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, account: MT5Account) -> MT5Account:
        self.session.add(account)
        await self.session.flush()
        await self.session.refresh(account)
        return account

    async def get(self, account_id: str) -> MT5Account | None:
        result = await self.session.execute(select(MT5Account).where(MT5Account.account_id == account_id))
        return result.scalars().first()

    async def get_by_login(self, login: int, server: str) -> MT5Account | None:
        result = await self.session.execute(
            select(MT5Account).where(MT5Account.login == login).where(MT5Account.server == server)
        )
        return result.scalars().first()

    async def list(self, enabled: bool | None = None, limit: int = 100, offset: int = 0) -> list[MT5Account]:
        statement = select(MT5Account)
        if enabled is not None:
            statement = statement.where(MT5Account.enabled == enabled)
        statement = statement.order_by(MT5Account.created_at.desc()).offset(offset).limit(limit)
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def due_for_sync(self, interval_minutes: int) -> list[MT5Account]:
        now = datetime.now(timezone.utc)
        threshold = now - timedelta(minutes=interval_minutes)
        statement = (
            _active(select(MT5Account))
            .where(MT5Account.last_synced_at.is_(None) | (MT5Account.last_synced_at < threshold))
            .where(MT5Account.history_withheld_until.is_(None) | (MT5Account.history_withheld_until <= now))
            .order_by(MT5Account.last_synced_at.asc().nullsfirst())
            .limit(_DUE_BATCH)
        )
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def counts_by_terminal(self) -> dict[str, int]:
        statement = (
            _active(select(MT5Account.terminal_path, func.count()))
            .where(MT5Account.terminal_path.is_not(None))
            .group_by(MT5Account.terminal_path)
        )
        result = await self.session.execute(statement)
        return {path: count for path, count in result.all()}

    async def update(self, account: MT5Account) -> MT5Account:
        account.updated_at = utc_now()
        self.session.add(account)
        await self.session.flush()
        await self.session.refresh(account)
        return account

    async def mark_synced(
        self,
        account: MT5Account,
        *,
        balance: float,
        equity: float,
        leverage: int,
        currency: str,
        server_clock: list | None = None,
    ) -> MT5Account:
        account.balance = balance
        account.equity = equity
        account.leverage = leverage
        account.currency = currency
        if server_clock:
            account.server_clock = server_clock
        account.status = AccountStatus.active
        account.consecutive_failures = 0
        account.last_error = None
        account.last_synced_at = utc_now()
        account.history_withheld_until = None
        return await self.update(account)

    async def mark_withheld(
        self,
        account: MT5Account,
        *,
        balance: float,
        equity: float,
        leverage: int,
        currency: str,
        server_clock: list | None = None,
        pause_minutes: int,
    ) -> MT5Account:
        account.balance = balance
        account.equity = equity
        account.leverage = leverage
        account.currency = currency
        if server_clock:
            account.server_clock = server_clock
        account.status = AccountStatus.active
        account.consecutive_failures = 0
        account.last_error = None
        account.last_synced_at = utc_now()
        account.history_withheld_until = utc_now() + timedelta(minutes=pause_minutes)
        return await self.update(account)

    async def mark_error(self, account: MT5Account, error: str, *, initial: bool, threshold: int) -> MT5Account:
        account.consecutive_failures += 1
        account.last_error = error
        if initial or account.consecutive_failures >= threshold:
            account.status = AccountStatus.error_connection
        return await self.update(account)

    async def delete(self, account: MT5Account) -> None:
        await self.session.delete(account)
        await self.session.flush()
