from __future__ import annotations

import time

from utils.logging import get_logger, log_event

from .clock import measure_server_clock
from .helpers.timeutil import as_naive_utc, history_range
from .raw import RawAccount, RawDeal, RawHistory, RawOrder, RawPosition
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


# How long a zero deal count is allowed to be the real answer before it is
# believed, and how many identical reads settle a non-zero one.
_ZERO_SETTLE_SECONDS = 5.0
_SETTLE_POLL_SECONDS = 1.0
_SETTLE_STABLE_READS = 2


def wait_for_history(
    terminal: MT5Terminal,
    account: RawAccount,
    *,
    timeout_seconds: float = 30.0,
) -> int:
    """Block until the terminal has finished pulling this account's history.

    ``login()`` returns before that download does. On a real account
    ``history_deals_total`` reads 0 for the first seconds and then jumps to the
    true figure - measured at 0 deals on return and 188 two seconds later.
    Reading in that window reconstructs an empty account, and worse, the ledger
    derives ``BalanceInit`` from a balance with no deals to explain it, so the
    damage is silently wrong numbers rather than a failure.

    A funded account cannot have zero deals - the deposit is itself a deal - so
    a zero count against a non-zero balance means the download is still in
    flight. An account that really is empty settles at zero and costs a few
    seconds.
    """
    start, end = history_range(None)
    start, end = as_naive_utc(start), as_naive_utc(end)
    started = time.monotonic()
    deadline = started + timeout_seconds

    previous: int | None = None
    stable = 0
    while True:
        total = terminal.mt5.history_deals_total(start, end)
        if total == previous:
            stable += 1
        else:
            previous, stable = total, 1

        if total > 0 and stable >= _SETTLE_STABLE_READS:
            break
        # Zero is only believed once the account has had a fair chance to
        # produce something, and never on an account that holds money.
        if total == 0 and not account.balance:
            if time.monotonic() - started >= _ZERO_SETTLE_SECONDS:
                break

        if time.monotonic() >= deadline:
            if total > 0:
                break
            if account.balance:
                raise TerminalError(
                    f"no deal history arrived for {account.login} in "
                    f"{timeout_seconds:.0f}s, yet the account holds "
                    f"{account.balance} {account.currency} - the terminal is "
                    f"still downloading, or the history is unavailable"
                )
            break

        time.sleep(_SETTLE_POLL_SECONDS)

    log_event(
        logger,
        "info",
        "terminal.history.settled",
        login=account.login,
        deals=total,
        waited_ms=int((time.monotonic() - started) * 1000),
    )
    return total


def fetch_history(
    terminal: MT5Terminal,
    *,
    settle_timeout_seconds: float = 30.0,
) -> RawHistory:
    account = fetch_account(terminal)
    wait_for_history(terminal, account, timeout_seconds=settle_timeout_seconds)
    deals = fetch_deals(terminal)
    orders = fetch_orders(terminal)
    open_positions = fetch_open_positions(terminal)
    # Measured from the symbols this account actually trades, so a broker
    # carrying none of the usual majors still gets an answer.
    # Measured from the symbols this account actually trades, so a broker
    # carrying none of the usual majors still gets an answer.
    server_offset = measure_server_clock(
        terminal,
        symbols=[deal.symbol for deal in reversed(deals) if deal.symbol][:4],
    ).offset_minutes
    open_orders = fetch_open_orders(terminal)

    log_event(
        logger,
        "info",
        "terminal.history.fetched",
        login=account.login,
        deals=len(deals),
        orders=len(orders),
        open_positions=len(open_positions),
        server_utc_offset_minutes=server_offset,
    )
    return RawHistory(
        account=account,
        deals=deals,
        orders=orders + open_orders,
        positions=open_positions,
        server_utc_offset_minutes=server_offset,
    )
