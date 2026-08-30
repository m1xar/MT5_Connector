from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from domain.enums import AccountStatus
from domain.models import MT5Account


class AccountCreateRequest(BaseModel):
    login: int = Field(description="MT5 account number")
    password: str = Field(description="MT5 password (investor or master)")
    server: str = Field(description="Broker server name exactly as the terminal lists it")
    broker: Optional[str] = None
    label: Optional[str] = None
    owner_id: Optional[str] = None


class AccountUpdateRequest(BaseModel):
    password: Optional[str] = None
    broker: Optional[str] = None
    label: Optional[str] = None
    enabled: Optional[bool] = None


class AccountResponse(BaseModel):
    account_id: str
    login: int
    server: str
    broker: Optional[str]
    label: Optional[str]
    owner_id: Optional[str]
    enabled: bool
    status: AccountStatus
    consecutive_failures: int
    server_utc_offset_minutes: Optional[int]
    balance: float
    equity: float
    leverage: int
    currency: str
    last_synced_at: Optional[str]
    last_error: Optional[str]
    created_at: Optional[str]


class AccountListResponse(BaseModel):
    accounts: List[AccountResponse]
    count: int


def account_to_response(account: MT5Account) -> AccountResponse:
    return AccountResponse(
        account_id=account.account_id,
        login=account.login,
        server=account.server,
        broker=account.broker,
        label=account.label,
        owner_id=account.owner_id,
        enabled=account.enabled,
        status=account.status,
        consecutive_failures=account.consecutive_failures,
        server_utc_offset_minutes=account.server_utc_offset_minutes,
        balance=account.balance,
        equity=account.equity,
        leverage=account.leverage,
        currency=account.currency,
        last_synced_at=account.last_synced_at.isoformat() if account.last_synced_at else None,
        last_error=account.last_error,
        created_at=account.created_at.isoformat() if account.created_at else None,
    )
