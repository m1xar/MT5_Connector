from __future__ import annotations

from domain import fx


def build_balance_snapshots(positions: list[fx.FXPosition]) -> list[fx.UserBalanceSnapshot]:
    closed = sorted(
        positions,
        key=lambda position: (position.closed_at is None, position.closed_at),
    )
    if not closed:
        return []

    snapshots = [
        fx.UserBalanceSnapshot(created_at=closed[0].created_at, balance=closed[0].balance_init)
    ]
    for position in closed:
        if position.closed_at is None:
            continue
        snapshots.append(
            fx.UserBalanceSnapshot(
                created_at=position.closed_at,
                balance=round(position.balance_init + position.net_pnl, 8),
            )
        )
    return snapshots
