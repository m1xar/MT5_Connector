from __future__ import annotations

from domain import fx

from .builders.open_position import build_open_positions
from .builders.position import build_fx_positions
from .builders.transactions import build_transactions
from .raw import RawHistory


def build_sync_payload(history: RawHistory) -> fx.SyncPayload:
    positions = build_fx_positions(
        deals=history.deals,
        orders=history.orders,
        current_balance=history.account.balance,
        leverage=history.account.leverage,
    )
    open_positions = build_open_positions(history.positions, history.orders)
    transactions = build_transactions(history.deals)

    clock = history.clock
    for position in positions:
        position.created_at_utc = clock.to_utc(position.created_at)
        position.closed_at_utc = clock.to_utc(position.closed_at)
    for open_position in open_positions:
        open_position.open_time_utc = clock.to_utc(open_position.open_time)
    for transaction in transactions:
        transaction.time_utc = clock.to_utc(transaction.time)

    return fx.SyncPayload(
        account_info=fx.FXAccountInfo(
            balance=history.account.balance,
            leverage=history.account.leverage,
            currency=history.account.currency,
        ),
        equity=history.account.equity,
        positions=positions,
        open_positions=open_positions,
        transactions=transactions,
        server_clock=clock.rows,
    )
