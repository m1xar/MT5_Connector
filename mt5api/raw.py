from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .clock import ServerClock

DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1
BALANCE_DEAL_TYPES = frozenset({2, 3, 4, 5, 6})

DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_INOUT = 2
DEAL_ENTRY_OUT_BY = 3

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_SELL_LIMIT = 3
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5
ORDER_TYPE_BUY_STOP_LIMIT = 6
ORDER_TYPE_SELL_STOP_LIMIT = 7
ORDER_TYPE_CLOSE_BY = 8

ORDER_STATE_CANCELED = 2
ORDER_STATE_FILLED = 4
ORDER_STATE_REJECTED = 5
ORDER_STATE_EXPIRED = 6

POSITION_TYPE_SELL = 1

TIMEFRAME_M1 = 1
TIMEFRAME_D1 = 16408


def _int(source: Any, name: str) -> int:
    return int(getattr(source, name, 0) or 0)


def _float(source: Any, name: str) -> float:
    return float(getattr(source, name, 0.0) or 0.0)


def _str(source: Any, name: str) -> str:
    return str(getattr(source, name, "") or "")


def _stamp(msc: int, seconds: int) -> datetime | None:
    if msc:
        return datetime.fromtimestamp(msc / 1000.0, tz=timezone.utc).replace(tzinfo=None)
    if seconds:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
    return None


@dataclass(slots=True)
class RawDeal:
    ticket: int = 0
    order: int = 0
    position_id: int = 0
    time: datetime | None = None
    time_msc: int = 0
    type: int = 0
    entry: int = 0
    volume: float = 0.0
    price: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    profit: float = 0.0
    fee: float = 0.0
    symbol: str = ""

    @property
    def is_trading(self) -> bool:
        return self.type in (DEAL_TYPE_BUY, DEAL_TYPE_SELL)

    @property
    def is_opening(self) -> bool:
        return self.entry in (DEAL_ENTRY_IN, DEAL_ENTRY_INOUT)

    @property
    def is_closing(self) -> bool:
        return self.entry in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY, DEAL_ENTRY_INOUT)

    @property
    def balance_delta(self) -> float:
        return self.profit + self.swap + self.commission + self.fee

    @classmethod
    def from_mt5(cls, row: Any) -> "RawDeal":
        msc, seconds = _int(row, "time_msc"), _int(row, "time")
        return cls(
            ticket=_int(row, "ticket"),
            order=_int(row, "order"),
            position_id=_int(row, "position_id"),
            time=_stamp(msc, seconds),
            time_msc=msc or seconds * 1000,
            type=_int(row, "type"),
            entry=_int(row, "entry"),
            volume=_float(row, "volume"),
            price=_float(row, "price"),
            commission=_float(row, "commission"),
            swap=_float(row, "swap"),
            profit=_float(row, "profit"),
            fee=_float(row, "fee"),
            symbol=_str(row, "symbol"),
        )


@dataclass(slots=True)
class RawOrder:
    ticket: int = 0
    position_id: int = 0
    time_setup: datetime | None = None
    time_done: datetime | None = None
    type: int = 0
    state: int = 0
    volume_initial: float = 0.0
    volume_current: float = 0.0
    price_open: float = 0.0
    price_stoplimit: float = 0.0
    sl: float = 0.0
    tp: float = 0.0

    @classmethod
    def from_mt5(cls, row: Any) -> "RawOrder":
        return cls(
            ticket=_int(row, "ticket"),
            position_id=_int(row, "position_id"),
            time_setup=_stamp(_int(row, "time_setup_msc"), _int(row, "time_setup")),
            time_done=_stamp(_int(row, "time_done_msc"), _int(row, "time_done")),
            type=_int(row, "type"),
            state=_int(row, "state"),
            volume_initial=_float(row, "volume_initial"),
            volume_current=_float(row, "volume_current"),
            price_open=_float(row, "price_open"),
            price_stoplimit=_float(row, "price_stoplimit"),
            sl=_float(row, "sl"),
            tp=_float(row, "tp"),
        )


@dataclass(slots=True)
class RawPosition:
    ticket: int = 0
    identifier: int = 0
    time: datetime | None = None
    type: int = 0
    volume: float = 0.0
    price_open: float = 0.0
    price_current: float = 0.0
    symbol: str = ""

    @classmethod
    def from_mt5(cls, row: Any) -> "RawPosition":
        return cls(
            ticket=_int(row, "ticket"),
            identifier=_int(row, "identifier"),
            time=_stamp(_int(row, "time_msc"), _int(row, "time")),
            type=_int(row, "type"),
            volume=_float(row, "volume"),
            price_open=_float(row, "price_open"),
            price_current=_float(row, "price_current"),
            symbol=_str(row, "symbol"),
        )


@dataclass(slots=True)
class RawCandle:
    time: datetime | None = None
    high: float = 0.0
    low: float = 0.0

    @classmethod
    def from_mt5(cls, row: Any) -> "RawCandle":
        return cls(
            time=_stamp(0, int(row["time"] or 0)),
            high=float(row["high"] or 0.0),
            low=float(row["low"] or 0.0),
        )


@dataclass(slots=True)
class RawAccount:
    login: int = 0
    balance: float = 0.0
    equity: float = 0.0
    leverage: int = 0
    currency: str = ""
    server: str = ""

    @classmethod
    def from_mt5(cls, row: Any) -> "RawAccount":
        return cls(
            login=_int(row, "login"),
            balance=_float(row, "balance"),
            equity=_float(row, "equity"),
            leverage=_int(row, "leverage"),
            currency=_str(row, "currency"),
            server=_str(row, "server"),
        )


@dataclass(slots=True)
class RawHistory:
    account: RawAccount = field(default_factory=RawAccount)
    deals: list[RawDeal] = field(default_factory=list)
    orders: list[RawOrder] = field(default_factory=list)
    positions: list[RawPosition] = field(default_factory=list)
    clock: ServerClock = field(default_factory=ServerClock)
