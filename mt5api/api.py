from __future__ import annotations

from domain import fx
from domain.payload import SyncPayload

from .builders.balance_snapshots import build_balance_snapshots
from .builders.open_position import build_open_positions
from .builders.position import build_fx_positions
from .builders.transactions import build_transactions
from .helpers.timeutil import cutoff_from_days, is_after_cutoff
from .raw import RawHistory


def build_sync_payload(history: RawHistory) -> SyncPayload:
    account_info = fx.FXAccountInfo(
        balance=history.account.balance,
        leverage=history.account.leverage,
        currency=history.account.currency,
    )
    positions = build_fx_positions(
        deals=history.deals,
        orders=history.orders,
        current_balance=history.account.balance,
        leverage=history.account.leverage,
    )
    return SyncPayload(
        account_info=account_info,
        equity=history.account.equity,
        positions=positions,
        open_positions=build_open_positions(history.positions, history.orders),
        transactions=build_transactions(history.deals),
    )


def filter_positions(positions: list[fx.FXPosition], days: int | None) -> list[fx.FXPosition]:
    cutoff = cutoff_from_days(days)
    if cutoff is None:
        return list(positions)
    return [position for position in positions if is_after_cutoff(position.closed_at, cutoff)]


def balance_snapshots(
    positions: list[fx.FXPosition],
    days: int | None = None,
) -> list[fx.UserBalanceSnapshot]:
    snapshots = build_balance_snapshots(positions)
    cutoff = cutoff_from_days(days)
    if cutoff is not None:
        snapshots = [item for item in snapshots if is_after_cutoff(item.created_at, cutoff)]
    snapshots.sort(key=lambda item: (item.created_at is None, item.created_at))
    return snapshots


def filter_transactions(
    transactions: list[fx.Transaction],
    days: int | None,
) -> list[fx.Transaction]:
    cutoff = cutoff_from_days(days)
    if cutoff is None:
        return list(transactions)
    return [item for item in transactions if is_after_cutoff(item.time, cutoff)]
