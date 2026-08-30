from __future__ import annotations

from domain import fx

from ..clock import ServerClock
from ..helpers.timeutil import cutoff_from_days, is_after_cutoff


def build_balance_snapshots(
    positions: list[fx.FXPosition],
    days: int | None = None,
    clock: ServerClock | None = None,
) -> list[fx.UserBalanceSnapshot]:
    """The account balance over time, derived rather than stored.

    One point for the balance the first position opened against, then one after
    each close. `days` trims the result rather than the input: the series has to
    be built from every position, or the first point would start mid-history.
    """
    clock = clock or ServerClock()
    closed = sorted(
        positions,
        key=lambda position: (position.closed_at is None, position.closed_at),
    )
    if not closed:
        return []

    snapshots = [
        fx.UserBalanceSnapshot(
            created_at=closed[0].created_at,
            created_at_utc=clock.to_utc(closed[0].created_at),
            balance=closed[0].balance_init,
        )
    ]
    snapshots += [
        fx.UserBalanceSnapshot(
            created_at=position.closed_at,
            created_at_utc=clock.to_utc(position.closed_at),
            balance=round(position.balance_init + position.net_pnl, 8),
        )
        for position in closed
        if position.closed_at is not None
    ]

    cutoff = cutoff_from_days(days)
    if cutoff is not None:
        snapshots = [s for s in snapshots if is_after_cutoff(s.created_at, cutoff)]
    snapshots.sort(key=lambda item: (item.created_at is None, item.created_at))
    return snapshots
