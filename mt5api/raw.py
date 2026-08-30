from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1
DEAL_TYPE_BALANCE = 2
DEAL_TYPE_CREDIT = 3
DEAL_TYPE_CHARGE = 4
DEAL_TYPE_CORRECTION = 5
DEAL_TYPE_BONUS = 6
DEAL_TYPE_COMMISSION = 7
DEAL_TYPE_COMMISSION_DAILY = 8
DEAL_TYPE_COMMISSION_MONTHLY = 9
DEAL_TYPE_COMMISSION_AGENT_DAILY = 10
DEAL_TYPE_COMMISSION_AGENT_MONTHLY = 11
DEAL_TYPE_INTEREST = 12
DEAL_TYPE_BUY_CANCELED = 13
DEAL_TYPE_SELL_CANCELED = 14
DEAL_TYPE_DIVIDEND = 15
DEAL_TYPE_DIVIDEND_FRANKED = 16
DEAL_TYPE_TAX = 17

TRADING_DEAL_TYPES = frozenset({DEAL_TYPE_BUY, DEAL_TYPE_SELL})

BALANCE_DEAL_TYPES = frozenset(
    {
        DEAL_TYPE_BALANCE,
        DEAL_TYPE_CREDIT,
        DEAL_TYPE_CHARGE,
        DEAL_TYPE_CORRECTION,
        DEAL_TYPE_BONUS,
    }
)

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

ORDER_STATE_STARTED = 0
ORDER_STATE_PLACED = 1
ORDER_STATE_CANCELED = 2
ORDER_STATE_PARTIAL = 3
ORDER_STATE_FILLED = 4
ORDER_STATE_REJECTED = 5
ORDER_STATE_EXPIRED = 6

POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1

TIMEFRAME_M1 = 1
TIMEFRAME_D1 = 16408


_MISSING = object()


def _get(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        value = source.get(name, default)
    else:
        value = getattr(source, name, _MISSING)
        if value is _MISSING:
            try:
                value = source[name]
            except (TypeError, IndexError, KeyError, ValueError):
                value = default
    return default if value is None else value


def _dt_from_msc(msc: int | None, seconds: int | None) -> datetime | None:
    """A timestamp in the trade server's clock, left unlabelled.

    MT5 hands these out as if they were UTC epochs, but they are stamped in the
    server's own clock. Marking them UTC would be a lie that survives all the
    way to the API, where a consumer would parse it confidently and be wrong by
    the offset. The `*Utc` twin is derived on the way out, once the offset is
    known, and carries the marker it has earned.
    """
    if msc:
        return datetime.fromtimestamp(int(msc) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
    if seconds:
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc).replace(tzinfo=None)
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
    magic: int = 0
    reason: int = 0
    volume: float = 0.0
    price: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    profit: float = 0.0
    fee: float = 0.0
    symbol: str = ""
    comment: str = ""
    external_id: str = ""

    @property
    def is_trading(self) -> bool:
        return self.type in TRADING_DEAL_TYPES

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
    def from_mt5(cls, source: Any) -> "RawDeal":
        time_msc = int(_get(source, "time_msc", 0) or 0)
        return cls(
            ticket=int(_get(source, "ticket", 0) or 0),
            order=int(_get(source, "order", 0) or 0),
            position_id=int(_get(source, "position_id", 0) or 0),
            time=_dt_from_msc(time_msc, int(_get(source, "time", 0) or 0)),
            time_msc=time_msc or int(_get(source, "time", 0) or 0) * 1000,
            type=int(_get(source, "type", 0) or 0),
            entry=int(_get(source, "entry", 0) or 0),
            magic=int(_get(source, "magic", 0) or 0),
            reason=int(_get(source, "reason", 0) or 0),
            volume=float(_get(source, "volume", 0.0) or 0.0),
            price=float(_get(source, "price", 0.0) or 0.0),
            commission=float(_get(source, "commission", 0.0) or 0.0),
            swap=float(_get(source, "swap", 0.0) or 0.0),
            profit=float(_get(source, "profit", 0.0) or 0.0),
            fee=float(_get(source, "fee", 0.0) or 0.0),
            symbol=str(_get(source, "symbol", "") or ""),
            comment=str(_get(source, "comment", "") or ""),
            external_id=str(_get(source, "external_id", "") or ""),
        )


@dataclass(slots=True)
class RawOrder:
    ticket: int = 0
    position_id: int = 0
    position_by_id: int = 0
    time_setup: datetime | None = None
    time_done: datetime | None = None
    type: int = 0
    state: int = 0
    magic: int = 0
    reason: int = 0
    volume_initial: float = 0.0
    volume_current: float = 0.0
    price_open: float = 0.0
    price_current: float = 0.0
    price_stoplimit: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    symbol: str = ""
    comment: str = ""

    @classmethod
    def from_mt5(cls, source: Any) -> "RawOrder":
        return cls(
            ticket=int(_get(source, "ticket", 0) or 0),
            position_id=int(_get(source, "position_id", 0) or 0),
            position_by_id=int(_get(source, "position_by_id", 0) or 0),
            time_setup=_dt_from_msc(
                int(_get(source, "time_setup_msc", 0) or 0),
                int(_get(source, "time_setup", 0) or 0),
            ),
            time_done=_dt_from_msc(
                int(_get(source, "time_done_msc", 0) or 0),
                int(_get(source, "time_done", 0) or 0),
            ),
            type=int(_get(source, "type", 0) or 0),
            state=int(_get(source, "state", 0) or 0),
            magic=int(_get(source, "magic", 0) or 0),
            reason=int(_get(source, "reason", 0) or 0),
            volume_initial=float(_get(source, "volume_initial", 0.0) or 0.0),
            volume_current=float(_get(source, "volume_current", 0.0) or 0.0),
            price_open=float(_get(source, "price_open", 0.0) or 0.0),
            price_current=float(_get(source, "price_current", 0.0) or 0.0),
            price_stoplimit=float(_get(source, "price_stoplimit", 0.0) or 0.0),
            sl=float(_get(source, "sl", 0.0) or 0.0),
            tp=float(_get(source, "tp", 0.0) or 0.0),
            symbol=str(_get(source, "symbol", "") or ""),
            comment=str(_get(source, "comment", "") or ""),
        )


@dataclass(slots=True)
class RawPosition:
    ticket: int = 0
    identifier: int = 0
    time: datetime | None = None
    type: int = 0
    magic: int = 0
    volume: float = 0.0
    price_open: float = 0.0
    price_current: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    swap: float = 0.0
    profit: float = 0.0
    symbol: str = ""
    comment: str = ""

    @classmethod
    def from_mt5(cls, source: Any) -> "RawPosition":
        return cls(
            ticket=int(_get(source, "ticket", 0) or 0),
            identifier=int(_get(source, "identifier", 0) or 0),
            time=_dt_from_msc(
                int(_get(source, "time_msc", 0) or 0),
                int(_get(source, "time", 0) or 0),
            ),
            type=int(_get(source, "type", 0) or 0),
            magic=int(_get(source, "magic", 0) or 0),
            volume=float(_get(source, "volume", 0.0) or 0.0),
            price_open=float(_get(source, "price_open", 0.0) or 0.0),
            price_current=float(_get(source, "price_current", 0.0) or 0.0),
            sl=float(_get(source, "sl", 0.0) or 0.0),
            tp=float(_get(source, "tp", 0.0) or 0.0),
            swap=float(_get(source, "swap", 0.0) or 0.0),
            profit=float(_get(source, "profit", 0.0) or 0.0),
            symbol=str(_get(source, "symbol", "") or ""),
            comment=str(_get(source, "comment", "") or ""),
        )


@dataclass(slots=True)
class RawCandle:

    time: datetime | None = None
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0

    @classmethod
    def from_mt5(cls, source: Any) -> "RawCandle":
        seconds = int(_get(source, "time", 0) or 0)
        return cls(
            time=_dt_from_msc(None, seconds),
            open=float(_get(source, "open", 0.0) or 0.0),
            high=float(_get(source, "high", 0.0) or 0.0),
            low=float(_get(source, "low", 0.0) or 0.0),
            close=float(_get(source, "close", 0.0) or 0.0),
            volume=float(_get(source, "tick_volume", 0.0) or 0.0),
        )


@dataclass(slots=True)
class RawAccount:
    login: int = 0
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    margin_free: float = 0.0
    leverage: int = 0
    currency: str = ""
    name: str = ""
    server: str = ""
    company: str = ""

    @classmethod
    def from_mt5(cls, source: Any) -> "RawAccount":
        return cls(
            login=int(_get(source, "login", 0) or 0),
            balance=float(_get(source, "balance", 0.0) or 0.0),
            equity=float(_get(source, "equity", 0.0) or 0.0),
            margin=float(_get(source, "margin", 0.0) or 0.0),
            margin_free=float(_get(source, "margin_free", 0.0) or 0.0),
            leverage=int(_get(source, "leverage", 0) or 0),
            currency=str(_get(source, "currency", "") or ""),
            name=str(_get(source, "name", "") or ""),
            server=str(_get(source, "server", "") or ""),
            company=str(_get(source, "company", "") or ""),
        )


@dataclass(slots=True)
class RawHistory:

    account: RawAccount = field(default_factory=RawAccount)
    deals: list[RawDeal] = field(default_factory=list)
    orders: list[RawOrder] = field(default_factory=list)
    positions: list[RawPosition] = field(default_factory=list)
    # Minutes the trade server's clock runs ahead of real UTC, or None when it
    # could not be measured. Every timestamp above is stamped in that clock.
    server_utc_offset_minutes: int | None = None
