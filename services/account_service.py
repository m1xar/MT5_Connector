from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from domain.enums import AccountStatus
from domain.models import MT5Account
from repositories.account_repo import AccountRepository
from repositories.position_repo import OpenPositionRepository, PositionRepository
from repositories.transaction_repo import TransactionRepository
from utils.logging import get_logger, log_event

logger = get_logger(__name__)


class AccountExistsError(ValueError):
    pass


class AccountService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = AccountRepository(session)

    async def create(
        self,
        *,
        login: int,
        password: str,
        server: str,
        broker: str | None = None,
        label: str | None = None,
        owner_id: str | None = None,
    ) -> MT5Account:
        existing = await self.repo.get_by_login(login, server)
        if existing is not None:
            raise AccountExistsError(f"account {login}@{server} already exists")

        account = await self.repo.create(
            MT5Account(
                login=login,
                password=password,
                server=server,
                broker=broker,
                label=label,
                owner_id=owner_id,
            )
        )
        await self.session.commit()
        log_event(logger, "info", "account.created", account_id=account.account_id, login=login)
        return account

    async def update(
        self,
        account: MT5Account,
        *,
        password: str | None = None,
        broker: str | None = None,
        label: str | None = None,
        enabled: bool | None = None,
    ) -> MT5Account:
        if password is not None:
            account.password = password
            # Fresh credentials are a fresh chance. Without this an account
            # that struck out would stay skipped by the scheduler even after
            # the very thing that broke it was fixed.
            account.status = AccountStatus.active
            account.consecutive_failures = 0
            account.last_error = None
        if broker is not None:
            account.broker = broker
        if label is not None:
            account.label = label
        if enabled is not None:
            # `enabled` decides whether the scheduler picks the account up;
            # `status` only ever reports whether the connection works.
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
