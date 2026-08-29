from __future__ import annotations

from utils.logging import get_logger, log_event

from datetime import datetime

from .helpers.timeutil import as_naive_utc, history_range
from .raw import (
    TIMEFRAME_D1,
    TIMEFRAME_M1,
    RawAccount,
    RawCandle,
    RawDeal,
    RawHistory,
    RawOrder,
    RawPosition,
)
from .terminal import MT5Terminal, TerminalError

logger = get_logger(__name__)


def fetch_account(terminal: MT5Terminal) -> RawAccount:
    info = terminal.mt5.account_info()
    if info is None:
        code, description = terminal.mt5.last_error()
        raise TerminalError(f"account_info failed: {description}", code=code)
    return RawAccount.from_mt5(info)


def fetch_deals(terminal: MT5Terminal, days: int | None = None) -> list[RawDeal]:
    start, end = history_range(days)
    rows = terminal.check_call(
        terminal.mt5.history_deals_get(as_naive_utc(start), as_naive_utc(end)),
        "history_deals_get",
    )
    return [RawDeal.from_mt5(row) for row in rows]


def fetch_orders(terminal: MT5Terminal, days: int | None = None) -> list[RawOrder]:
    start, end = history_range(days)
    rows = terminal.check_call(
        terminal.mt5.history_orders_get(as_naive_utc(start), as_naive_utc(end)),
        "history_orders_get",
    )
    return [RawOrder.from_mt5(row) for row in rows]


def fetch_open_positions(terminal: MT5Terminal) -> list[RawPosition]:
    rows = terminal.check_call(terminal.mt5.positions_get(), "positions_get")
    return [RawPosition.from_mt5(row) for row in rows]


def fetch_open_orders(terminal: MT5Terminal) -> list[RawOrder]:
    rows = terminal.check_call(terminal.mt5.orders_get(), "orders_get")
    return [RawOrder.from_mt5(row) for row in rows]


_TIMEFRAMES = {"1m": TIMEFRAME_M1, "1d": TIMEFRAME_D1}


def fetch_candles(
    terminal: MT5Terminal,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> list[RawCandle]:
    timeframe = _TIMEFRAMES[interval]
    terminal.mt5.symbol_select(symbol, True)
    rows = terminal.check_call(
        terminal.mt5.copy_rates_range(
            symbol, timeframe, as_naive_utc(start), as_naive_utc(end)
        ),
        "copy_rates_range",
    )
    return [RawCandle.from_mt5(row) for row in rows]


def fetch_history(terminal: MT5Terminal) -> RawHistory:
    account = fetch_account(terminal)
    deals = fetch_deals(terminal)
    orders = fetch_orders(terminal)
    open_positions = fetch_open_positions(terminal)
    open_orders = fetch_open_orders(terminal)

    log_event(
        logger,
        "info",
        "terminal.history.fetched",
        login=account.login,
        deals=len(deals),
        orders=len(orders),
        open_positions=len(open_positions),
    )
    return RawHistory(
        account=account,
        deals=deals,
        orders=orders + open_orders,
        positions=open_positions,
    )
