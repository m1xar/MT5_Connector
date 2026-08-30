from __future__ import annotations

from domain import fx
from domain.payload import SyncPayload

from .builders.open_position import build_open_positions
from .builders.position import build_fx_positions
from .builders.transactions import build_transactions
from .raw import RawHistory


def build_sync_payload(history: RawHistory) -> SyncPayload:
    """Everything one sync produces, from one terminal read.

    Positions have to be rebuilt from the whole deal history rather than a
    window: the ledger recovers each position's opening balance by replaying
    every balance effect from the account's start, so a partial pull would
    recover the wrong starting balance. Trimming to a window is a read-side
    concern, and happens on the way out of the database instead.
    """
    return SyncPayload(
        account_info=fx.FXAccountInfo(
            balance=history.account.balance,
            leverage=history.account.leverage,
            currency=history.account.currency,
        ),
        equity=history.account.equity,
        positions=build_fx_positions(
            deals=history.deals,
            orders=history.orders,
            current_balance=history.account.balance,
            leverage=history.account.leverage,
        ),
        open_positions=build_open_positions(history.positions, history.orders),
        transactions=build_transactions(history.deals),
        server_clock=history.clock.rows,
    )
