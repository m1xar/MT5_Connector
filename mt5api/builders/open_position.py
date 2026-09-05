from __future__ import annotations

from domain import fx

from .. import raw
from .orders import build_orders


def build_open_positions(positions: list[raw.RawPosition], orders: list[raw.RawOrder]) -> list[fx.FXOpenPosition]:
    orders_by_position: dict[int, list[raw.RawOrder]] = {}
    for order in orders:
        if order.position_id:
            orders_by_position.setdefault(order.position_id, []).append(order)

    out: list[fx.FXOpenPosition] = []
    for position in positions:
        position_id = position.identifier or position.ticket
        key = str(position_id)
        out.append(fx.FXOpenPosition(
            id=key,
            pair=position.symbol,
            amount=position.volume,
            side=fx.SIDE_SHORT if position.type == raw.POSITION_TYPE_SELL else fx.SIDE_LONG,
            entry_price=position.price_open,
            current_price=position.price_current,
            open_time=position.time,
            orders=build_orders(orders_by_position.get(position_id, []), [], key),
        ))
    return out
