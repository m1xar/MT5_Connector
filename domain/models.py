from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import BigInteger, Column, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel

from utils.id import new_id

from .enums import AccountStatus

_JSON = JSON().with_variant(JSONB(), "postgresql")


def _utc_column(**kwargs: Any) -> Column:
    return Column(DateTime(timezone=True), **kwargs)


def utc_now() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)


class MT5Account(SQLModel, table=True):
    __tablename__ = "mt5account"
    __table_args__ = (UniqueConstraint("login", "server", name="uq_mt5account_login_server"),)

    account_id: str = Field(default_factory=new_id, primary_key=True)
    # MT5 login numbers run to ten digits, past the 2^31 an INTEGER holds.
    login: int = Field(sa_column=Column(BigInteger, index=True, nullable=False))
    password: str = Field()
    server: str = Field(index=True)
    broker: Optional[str] = Field(default=None)
    label: Optional[str] = Field(default=None)
    owner_id: Optional[str] = Field(default=None, index=True)

    enabled: bool = Field(default=True, index=True)
    status: AccountStatus = Field(default=AccountStatus.active, index=True)
    consecutive_failures: int = Field(default=0)

    balance: float = Field(default=0.0)
    equity: float = Field(default=0.0)
    leverage: int = Field(default=0)
    currency: str = Field(default="")
    # The trade server's clock as [server label, offset minutes] pairs, one
    # per daylight saving switch. Every timestamp MT5 hands out is stamped in
    # that clock, so this is what turns the stored times into real UTC on the
    # way out - with the offset that was in force at the time, not today's.
    server_clock: List[List[Any]] = Field(default_factory=list, sa_column=Column(_JSON))

    last_synced_at: Optional[datetime] = Field(default=None, sa_column=_utc_column(index=True))
    last_error: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now, sa_column=_utc_column(index=True))
    updated_at: datetime = Field(default_factory=utc_now, sa_column=_utc_column())


class MT5Position(SQLModel, table=True):

    __tablename__ = "mt5position"
    __table_args__ = (
        UniqueConstraint("account_id", "external_id", name="uq_mt5position_account_external"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    account_id: str = Field(foreign_key="mt5account.account_id", index=True)
    external_id: str = Field(index=True)

    side: str = Field()
    pair: str = Field(index=True)
    amount: float = Field(default=0.0)
    entry_price: float = Field(default=0.0)
    exit_price: float = Field(default=0.0)
    pnl: float = Field(default=0.0)
    net_pnl: float = Field(default=0.0)
    commission: float = Field(default=0.0)
    swap: float = Field(default=0.0)
    mae: Optional[float] = Field(default=None)
    mfe: Optional[float] = Field(default=None)
    rr: Optional[float] = Field(default=None)
    rr_planned: Optional[float] = Field(default=None)
    tp: Optional[float] = Field(default=None)
    sl: Optional[float] = Field(default=None)
    liquidation_price: float = Field(default=0.0)
    multiplier: int = Field(default=1)
    isolated: bool = Field(default=False)
    closed: bool = Field(default=True)
    status: Optional[str] = Field(default=None, index=True)
    balance_init: float = Field(default=0.0)

    created_at: Optional[datetime] = Field(default=None, sa_column=_utc_column(index=True))
    closed_at: Optional[datetime] = Field(default=None, sa_column=_utc_column(index=True))
    orders: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(_JSON))

    synced_at: datetime = Field(default_factory=utc_now, sa_column=_utc_column())


class MT5OpenPosition(SQLModel, table=True):

    __tablename__ = "mt5openposition"
    __table_args__ = (
        UniqueConstraint("account_id", "external_id", name="uq_mt5openposition_account_external"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    account_id: str = Field(foreign_key="mt5account.account_id", index=True)
    external_id: str = Field(index=True)

    pair: str = Field(index=True)
    amount: float = Field(default=0.0)
    side: str = Field()
    entry_price: float = Field(default=0.0)
    current_price: float = Field(default=0.0)
    open_time: Optional[datetime] = Field(default=None, sa_column=_utc_column(index=True))
    orders: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(_JSON))

    synced_at: datetime = Field(default_factory=utc_now, sa_column=_utc_column())


class MT5Transaction(SQLModel, table=True):

    __tablename__ = "mt5transaction"
    __table_args__ = (
        UniqueConstraint("account_id", "fingerprint", name="uq_mt5transaction_fingerprint"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    account_id: str = Field(foreign_key="mt5account.account_id", index=True)
    fingerprint: str = Field(index=True)

    time: Optional[datetime] = Field(default=None, sa_column=_utc_column(index=True))
    type: str = Field(index=True)
    amount: float = Field(default=0.0)

    synced_at: datetime = Field(default_factory=utc_now, sa_column=_utc_column())
