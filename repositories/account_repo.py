from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from domain.enums import AccountStatus
from domain.models import MT5Account, utc_now


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, account: MT5Account) -> MT5Account:
        self.session.add(account)
        await self.session.flush()
        await self.session.refresh(account)
        return account

    async def get(self, account_id: str) -> Optional[MT5Account]:
        result = await self.session.execute(
            select(MT5Account).where(MT5Account.account_id == account_id)
        )
        return result.scalars().first()

    async def get_by_login(self, login: int, server: str) -> Optional[MT5Account]:
        result = await self.session.execute(
            select(MT5Account).where(MT5Account.login == login).where(MT5Account.server == server)
        )
        return result.scalars().first()

    async def list(
        self,
        owner_id: str | None = None,
        enabled: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[MT5Account]:
        statement = select(MT5Account)
        if owner_id is not None:
            statement = statement.where(MT5Account.owner_id == owner_id)
        if enabled is not None:
            statement = statement.where(MT5Account.enabled == enabled)
        statement = statement.order_by(MT5Account.created_at.desc()).offset(offset).limit(limit)
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def due_for_sync(self, interval_minutes: int, limit: int = 200) -> List[MT5Account]:
        threshold = datetime.now(timezone.utc) - timedelta(minutes=interval_minutes)
        statement = (
            select(MT5Account)
            .where(MT5Account.enabled == True)
            # An account that has never connected is due on every single
            # tick, so leaving broken ones in would let one bad server name
            # occupy the whole pool forever. They come back on an explicit
            # sync, or when their credentials are updated.
            .where(MT5Account.status != AccountStatus.error_connection)
            .where(
                (MT5Account.last_synced_at == None)
                | (MT5Account.last_synced_at < threshold)
            )
            .order_by(MT5Account.last_synced_at.asc().nullsfirst())
            .limit(limit)
        )
        result = await self.session.execute(statement)
        return list(result.scalars().all())

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
        server_utc_offset_minutes: int | None = None,
    ) -> MT5Account:
        account.balance = balance
        account.equity = equity
        account.leverage = leverage
        account.currency = currency
        # Left alone when this sync could not measure it - a weekend must not
        # erase a offset that was measured perfectly well on Friday.
        if server_utc_offset_minutes is not None:
            account.server_utc_offset_minutes = server_utc_offset_minutes
        account.status = AccountStatus.active
        account.consecutive_failures = 0
        account.last_error = None
        account.last_synced_at = utc_now()
        return await self.update(account)

    async def mark_error(
        self,
        account: MT5Account,
        error: str,
        *,
        initial: bool = False,
        threshold: int = 3,
    ) -> MT5Account:
        """Count the failure, and flip the status once it stops looking like a blip.

        A failed *first* sync is conclusive on its own: the account was only
        just added and has never once connected, so there is no run of
        successes for this to be a blip in.
        """
        account.consecutive_failures += 1
        account.last_error = error
        if initial or account.consecutive_failures >= threshold:
            account.status = AccountStatus.error_connection
        return await self.update(account)

    async def delete(self, account: MT5Account) -> None:
        await self.session.delete(account)
        await self.session.flush()
