from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from domain.enums import AccountStatus
from domain.models import MT5Account
from repositories.account_repo import AccountRepository
from repositories.position_repo import OpenPositionRepository, PositionRepository
from repositories.transaction_repo import TransactionRepository
from utils.logging import log_event

logger = logging.getLogger(__name__)


class AccountExistsError(ValueError):
    pass


class AccountService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = AccountRepository(session)

    async def create(self, *, login: int, password: str, server: str) -> MT5Account:
        if await self.repo.get_by_login(login, server) is not None:
            raise AccountExistsError(f"account {login}@{server} already exists")
        account = await self.repo.create(MT5Account(login=login, password=password, server=server))
        await self.session.commit()
        log_event(logger, "info", "account.created", account_id=account.account_id, login=login)
        return account

    async def update(
        self, account: MT5Account, *, password: str | None = None, enabled: bool | None = None
    ) -> MT5Account:
        if password is not None:
            account.password = password
            account.status = AccountStatus.active
            account.consecutive_failures = 0
            account.last_error = None
        if enabled is not None:
            account.enabled = enabled
        updated = await self.repo.update(account)
        await self.session.commit()
        log_event(logger, "info", "account.updated", account_id=account.account_id)
        return updated

    async def delete(self, account: MT5Account) -> None:
        account_id = account.account_id
        await PositionRepository(self.session).delete_for_account(account_id)
        await OpenPositionRepository(self.session).delete_for_account(account_id)
        await TransactionRepository(self.session).delete_for_account(account_id)
        await self.repo.delete(account)
        await self.session.commit()
        log_event(logger, "info", "account.deleted", account_id=account_id)
