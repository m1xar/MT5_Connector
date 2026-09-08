from __future__ import annotations

import logging
import time
from datetime import datetime

from utils.logging import log_event

from .clock import ServerClock, measure_server_clock
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
from .enrichment import DAY, MINUTE
from .terminal import MT5Terminal, is_ipc
from .timeutil import as_mt5_time, as_server_time, history_range

logger = logging.getLogger(__name__)

_SETTLE_POLL_SECONDS = 1.0
_SETTLE_STABLE_READS = 2

_CLOCK_TTL_SCANNED = 6 * 3600.0
_CLOCK_TTL_TICK_ONLY = 300.0
_CLOCK_TTL_UNKNOWN = 6 * 3600.0
_CLOCK_SYMBOLS = 4
_clock_cache: dict[str, tuple[float, ServerClock]] = {}
_clock_misses: dict[str, float] = {}

_TIMEFRAMES = {MINUTE: TIMEFRAME_M1, DAY: TIMEFRAME_D1}


def fetch_account(terminal: MT5Terminal) -> RawAccount:
    info = terminal.mt5.account_info()
    if info is None:
        code, description = terminal.mt5.last_error()
        raise terminal.failure(f"account_info failed: {description}", code)
    return RawAccount.from_mt5(info)


def fetch_deals(terminal: MT5Terminal, start: datetime, end: datetime) -> list[RawDeal]:
    rows = terminal.check_call(terminal.mt5.history_deals_get(start, end), "history_deals_get")
    return [RawDeal.from_mt5(row) for row in rows]


def fetch_orders(terminal: MT5Terminal, start: datetime, end: datetime) -> list[RawOrder]:
    rows = terminal.check_call(terminal.mt5.history_orders_get(start, end), "history_orders_get")
    return [RawOrder.from_mt5(row) for row in rows]


def fetch_open_positions(terminal: MT5Terminal) -> list[RawPosition]:
    rows = terminal.check_call(terminal.mt5.positions_get(), "positions_get")
    return [RawPosition.from_mt5(row) for row in rows]


def fetch_open_orders(terminal: MT5Terminal) -> list[RawOrder]:
    rows = terminal.check_call(terminal.mt5.orders_get(), "orders_get")
    return [RawOrder.from_mt5(row) for row in rows]


def wait_for_history(
    terminal: MT5Terminal, account: RawAccount, start: datetime, end: datetime, *, timeout_seconds: float
) -> None:
    started = time.monotonic()
    deadline = started + timeout_seconds
    previous: int | None = None
    stable = 0
    while True:
        total = terminal.mt5.history_deals_total(start, end)
        if total is None:
            code, description = terminal.mt5.last_error()
            if is_ipc(code):
                raise terminal.failure(f"history_deals_total failed: {description}", code)
        elif total == previous:
            stable += 1
        else:
            previous, stable = total, 1
        if (total and stable >= _SETTLE_STABLE_READS) or time.monotonic() >= deadline:
            break
        time.sleep(_SETTLE_POLL_SECONDS)

    log_event(
        logger, "info", "terminal.history.settled",
        login=account.login, deals=total, waited_ms=int((time.monotonic() - started) * 1000),
    )


def history_withheld(history: RawHistory) -> bool:
    if history.deals:
        return False
    account = history.account
    round_balance = abs(account.balance - round(account.balance, -1)) <= 0.005
    return bool(history.positions) or not round_balance or account.equity != account.balance


def _clock_symbols(deals: list[RawDeal]) -> list[str]:
    seen: list[str] = []
    for deal in reversed(deals):
        if deal.symbol and deal.symbol not in seen:
            seen.append(deal.symbol)
            if len(seen) == _CLOCK_SYMBOLS:
                break
    return seen


def fetch_clock(terminal: MT5Terminal, server: str, symbols: list[str]) -> ServerClock:
    now = time.monotonic()
    cached = _clock_cache.get(server)
    if cached:
        ttl = _CLOCK_TTL_SCANNED if cached[1].scanned else _CLOCK_TTL_TICK_ONLY
        if now - cached[0] < ttl:
            log_event(
                logger, "info", "terminal.clock.reused",
                server=server, offset_minutes=cached[1].offset_minutes, scanned=cached[1].scanned,
                age_seconds=int(now - cached[0]),
            )
            return cached[1]

    missed = _clock_misses.get(server)
    if missed is not None and now - missed < _CLOCK_TTL_UNKNOWN:
        log_event(
            logger, "info", "terminal.clock.unknown.reused",
            server=server, symbols=symbols, age_seconds=int(now - missed), stale_reading=cached is not None,
        )
        return cached[1] if cached else ServerClock()

    clock = measure_server_clock(terminal, symbols=symbols)
    if clock.known:
        _clock_cache[server] = (now, clock)
        _clock_misses.pop(server, None)
        return clock

    _clock_misses[server] = now
    if cached is not None:
        log_event(
            logger, "warning", "terminal.clock.stale_kept",
            server=server, offset_minutes=cached[1].offset_minutes, age_seconds=int(now - cached[0]),
        )
        return cached[1]
    return clock


def fetch_history(terminal: MT5Terminal, *, settle_timeout_seconds: float) -> RawHistory:
    start, end = (as_mt5_time(edge) for edge in history_range())
    account = fetch_account(terminal)
    wait_for_history(terminal, account, start, end, timeout_seconds=settle_timeout_seconds)
    deals = fetch_deals(terminal, start, end)
    orders = fetch_orders(terminal, start, end)
    open_positions = fetch_open_positions(terminal)
    clock = fetch_clock(terminal, account.server, _clock_symbols(deals))
    open_orders = fetch_open_orders(terminal)
    log_event(
        logger, "info", "terminal.history.fetched",
        login=account.login, deals=len(deals), orders=len(orders), open_positions=len(open_positions),
        server_utc_offset_minutes=clock.offset_minutes,
    )
    return RawHistory(
        account=account, deals=deals, orders=orders + open_orders, positions=open_positions, clock=clock,
    )


def fetch_candles(
    terminal: MT5Terminal, symbol: str, interval: str, start: datetime, end: datetime
) -> list[RawCandle]:
    terminal.mt5.symbol_select(symbol, True)
    rows = terminal.check_call(
        terminal.mt5.copy_rates_range(symbol, _TIMEFRAMES[interval], as_mt5_time(start), as_mt5_time(end)),
        "copy_rates_range",
    )
    window_start, window_end = as_server_time(start), as_server_time(end)
    candles = [RawCandle.from_mt5(row) for row in rows]
    inside = [c for c in candles if c.time is not None and window_start <= c.time <= window_end]
    if len(inside) != len(candles):
        log_event(
            logger, "warning" if not inside else "debug", "terminal.candles.out_of_range",
            symbol=symbol, interval=interval, window_start=window_start.isoformat(),
            window_end=window_end.isoformat(), returned=len(candles), kept=len(inside),
        )
    return inside
