from __future__ import annotations

from domain import fx

from .. import raw
from .orders import build_orders

_POSITION_TYPE_TO_DEAL_TYPE = {
    raw.POSITION_TYPE_BUY: raw.DEAL_TYPE_BUY,
    raw.POSITION_TYPE_SELL: raw.DEAL_TYPE_SELL,
}


def build_open_positions(
    positions: list[raw.RawPosition],
    orders: list[raw.RawOrder],
) -> list[fx.FXOpenPosition]:
    orders_by_position: dict[int, list[raw.RawOrder]] = {}
    for order in orders:
        if not order.position_id:
            continue
        orders_by_position.setdefault(order.position_id, []).append(order)

    out: list[fx.FXOpenPosition] = []
    for position in positions:
        position_id = position.identifier or position.ticket
        key = str(position_id)
        deal_type = _POSITION_TYPE_TO_DEAL_TYPE.get(position.type, raw.DEAL_TYPE_BUY)
        out.append(
            fx.FXOpenPosition(
                id=key,
                pair=position.symbol,
                amount=position.volume,
                side=fx.SIDE_LONG if deal_type == raw.DEAL_TYPE_BUY else fx.SIDE_SHORT,
                entry_price=position.price_open,
                current_price=position.price_current,
                open_time=position.time,
                orders=build_orders(orders_by_position.get(position_id, []), [], key),
            )
        )
    return out
