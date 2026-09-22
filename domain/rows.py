from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import fx

POSITION_FIELDS = (
    "side", "pair", "amount", "entry_price", "exit_price", "pnl", "net_pnl", "commission", "swap",
    "mae", "mfe", "rr", "rr_planned", "tp", "sl", "liquidation_price", "multiplier", "isolated",
    "closed", "status", "balance_init", "created_at", "closed_at",
)
OPEN_POSITION_FIELDS = ("pair", "amount", "side", "entry_price", "current_price", "open_time")


@dataclass(slots=True)
class SyncRows:
    balance: float = 0.0
    equity: float = 0.0
    leverage: int = 0
    currency: str = ""
    server_clock: list = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    transactions: list[dict[str, Any]] = field(default_factory=list)


def transaction_fingerprint(time: datetime | None, kind: str, amount: float) -> str:
    stamp = time.isoformat() if time else ""
    return hashlib.sha256(f"{stamp}|{kind}|{amount:.8f}".encode("utf-8")).hexdigest()[:16]


def orders_json(orders: list[fx.FXOrder]) -> list[dict]:
    return [order.model_dump(by_alias=True, mode="json") for order in orders]


def position_row(position: fx.FXPosition) -> dict[str, Any]:
    row = {name: getattr(position, name) for name in POSITION_FIELDS}
    row.update(
        external_id=position.id, orders=orders_json(position.orders),
        created_at_utc=position.created_at_utc, closed_at_utc=position.closed_at_utc,
    )
    return row


def position_from_row(row: dict[str, Any]) -> fx.FXPosition:
    return fx.FXPosition(
        id=row["external_id"],
        orders=[fx.FXOrder.model_validate(order) for order in row["orders"]],
        created_at_utc=row.get("created_at_utc"),
        closed_at_utc=row.get("closed_at_utc"),
        **{name: row[name] for name in POSITION_FIELDS},
    )


def open_position_row(position: fx.FXOpenPosition) -> dict[str, Any]:
    row = {name: getattr(position, name) for name in OPEN_POSITION_FIELDS}
    row.update(external_id=position.id, orders=orders_json(position.orders), open_time_utc=position.open_time_utc)
    return row


def open_position_from_row(row: dict[str, Any]) -> fx.FXOpenPosition:
    return fx.FXOpenPosition(
        id=row["external_id"],
        orders=[fx.FXOrder.model_validate(order) for order in row["orders"]],
        open_time_utc=row.get("open_time_utc"),
        **{name: row[name] for name in OPEN_POSITION_FIELDS},
    )


def transaction_row(transaction: fx.Transaction) -> dict[str, Any]:
    return {
        "fingerprint": transaction_fingerprint(transaction.time, transaction.type, transaction.amount),
        "time": transaction.time, "time_utc": transaction.time_utc,
        "type": transaction.type, "amount": transaction.amount,
    }


def transaction_from_row(row: dict[str, Any]) -> fx.Transaction:
    return fx.Transaction(time=row["time"], time_utc=row.get("time_utc"), type=row["type"], amount=row["amount"])


def rows_from_payload(payload: fx.SyncPayload) -> SyncRows:
    return SyncRows(
        balance=payload.account_info.balance,
        equity=payload.equity,
        leverage=payload.account_info.leverage,
        currency=payload.account_info.currency,
        server_clock=payload.server_clock,
        positions=[position_row(position) for position in payload.positions],
        open_positions=[open_position_row(position) for position in payload.open_positions],
        transactions=[transaction_row(transaction) for transaction in payload.transactions],
    )


def payload_from_rows(rows: SyncRows) -> fx.SyncPayload:
    return fx.SyncPayload(
        account_info=fx.FXAccountInfo(balance=rows.balance, leverage=rows.leverage, currency=rows.currency),
        equity=rows.equity,
        server_clock=rows.server_clock,
        positions=[position_from_row(row) for row in rows.positions],
        open_positions=[open_position_from_row(row) for row in rows.open_positions],
        transactions=[transaction_from_row(row) for row in rows.transactions],
    )
