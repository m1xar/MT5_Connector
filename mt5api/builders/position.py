from __future__ import annotations

from domain import fx

from .. import raw
from ..helpers.ledger import balance_after_close, sort_deals
from ..helpers.mathutil import abs8, round8, weighted_price
from .enrich import apply_rr
from .orders import build_orders, build_orders_from_deals, trade_side


def build_fx_positions(
    deals: list[raw.RawDeal],
    orders: list[raw.RawOrder],
    current_balance: float,
    leverage: int,
) -> list[fx.FXPosition]:
    grouped_deals: dict[int, list[raw.RawDeal]] = {}
    for deal in deals:
        if not deal.is_trading or not deal.position_id:
            continue
        grouped_deals.setdefault(deal.position_id, []).append(deal)

    grouped_orders: dict[int, list[raw.RawOrder]] = {}
    for order in orders:
        if not order.position_id:
            continue
        grouped_orders.setdefault(order.position_id, []).append(order)

    close_balances = balance_after_close(deals, current_balance)

    positions: list[fx.FXPosition] = []
    for position_id, position_deals in grouped_deals.items():
        ordered = sort_deals(position_deals)
        if not any(deal.is_closing for deal in ordered):
            continue
        position = _build_fx_position(
            position_id=position_id,
            deals=ordered,
            orders=grouped_orders.get(position_id, []),
            balance_after=close_balances.get(position_id, 0.0),
            leverage=leverage,
        )
        apply_rr(position)
        positions.append(position)

    positions.sort(key=lambda position: (position.created_at is None, position.created_at))
    return positions


def _build_fx_position(
    position_id: int,
    deals: list[raw.RawDeal],
    orders: list[raw.RawOrder],
    balance_after: float,
    leverage: int,
) -> fx.FXPosition:
    position_key = str(position_id)
    opening = [deal for deal in deals if deal.is_opening]
    closing = [deal for deal in deals if deal.is_closing]
    first_open = opening[0] if opening else deals[0]
    last_close = closing[-1]

    pnl = sum(deal.profit for deal in closing)
    swap = sum(deal.swap for deal in deals)
    commission = abs8(sum(deal.commission for deal in deals))
    fee = abs8(sum(deal.fee for deal in deals))

    net = pnl + swap - commission - fee
    status = fx.STATUS_WIN if net > 0 else fx.STATUS_LOSE

    entry_price = weighted_price([(deal.volume, deal.price) for deal in opening])
    exit_price = weighted_price([(deal.volume, deal.price) for deal in closing])

    position_orders = build_orders(orders, deals, position_key)
    if not position_orders:
        position_orders = build_orders_from_deals(deals, position_key)

    take_profit, stop_loss = _extract_protection(orders)

    return fx.FXPosition(
        id=position_key,
        side=trade_side(first_open.type),
        pair=first_open.symbol or last_close.symbol,
        amount=round8(sum(deal.volume for deal in closing)),
        entry_price=entry_price,
        exit_price=exit_price,
        pnl=round8(pnl),
        net_pnl=round8(net),
        commission=commission,
        swap=round8(swap),
        tp=take_profit,
        sl=stop_loss,
        multiplier=leverage if leverage > 0 else 1,
        closed=True,
        status=status,
        created_at=first_open.time,
        closed_at=last_close.time,
        orders=position_orders,
        balance_init=round8(balance_after - net),
    )


def _extract_protection(orders: list[raw.RawOrder]) -> tuple[float | None, float | None]:
    ordered = sorted(orders, key=lambda order: (order.time_setup is None, order.time_setup))
    take_profit: float | None = None
    stop_loss: float | None = None
    for order in ordered:
        if take_profit is None and order.tp:
            take_profit = order.tp
        if stop_loss is None and order.sl:
            stop_loss = order.sl
    return take_profit, stop_loss
