from __future__ import annotations

from datetime import datetime

from domain import fx

from ..clock import ServerClock


def build_balance_snapshots(
    positions: list[fx.FXPosition],
    since: datetime | None = None,
    clock: ServerClock | None = None,
) -> list[fx.UserBalanceSnapshot]:
    clock = clock or ServerClock()
    closed = sorted(positions, key=lambda position: (position.closed_at is None, position.closed_at))
    if not closed:
        return []

    snapshots = [fx.UserBalanceSnapshot(
        created_at=closed[0].created_at,
        created_at_utc=clock.to_utc(closed[0].created_at),
        balance=closed[0].balance_init,
    )]
    snapshots += [
        fx.UserBalanceSnapshot(
            created_at=position.closed_at,
            created_at_utc=clock.to_utc(position.closed_at),
            balance=round(position.balance_init + position.net_pnl, 8),
        )
        for position in closed
        if position.closed_at is not None
    ]
    if since is not None:
        snapshots = [s for s in snapshots if s.created_at is not None and s.created_at >= since]
    snapshots.sort(key=lambda item: (item.created_at is None, item.created_at))
    return snapshots
