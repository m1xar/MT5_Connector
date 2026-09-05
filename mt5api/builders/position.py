from __future__ import annotations

from domain import fx

from .. import raw
from ..enrichment import apply_rr, resolve_value_per_price_unit, value_per_lot
from ..numbers import abs8, round8, weighted_price
from .orders import build_orders, build_orders_from_deals, trade_side


def sort_deals(deals: list[raw.RawDeal]) -> list[raw.RawDeal]:
    return sorted(deals, key=lambda deal: (deal.time_msc, deal.ticket))


def balance_after_close(deals: list[raw.RawDeal], current_balance: float) -> dict[int, float]:
    running = round8(current_balance - sum(deal.balance_delta for deal in deals))
    balances: dict[int, float] = {}
    for deal in sort_deals(deals):
        running = round8(running + deal.balance_delta)
        if deal.is_trading and deal.position_id and deal.is_closing:
            balances[deal.position_id] = running
    return balances


def build_fx_positions(
    deals: list[raw.RawDeal],
    orders: list[raw.RawOrder],
    current_balance: float,
    leverage: int,
) -> list[fx.FXPosition]:
    grouped_deals: dict[int, list[raw.RawDeal]] = {}
    for deal in deals:
        if deal.is_trading and deal.position_id:
            grouped_deals.setdefault(deal.position_id, []).append(deal)

    grouped_orders: dict[int, list[raw.RawOrder]] = {}
    for order in orders:
        if order.position_id:
            grouped_orders.setdefault(order.position_id, []).append(order)

    close_balances = balance_after_close(deals, current_balance)

    positions: list[fx.FXPosition] = []
    for position_id, position_deals in grouped_deals.items():
        ordered = sort_deals(position_deals)
        if not any(deal.is_closing for deal in ordered):
            continue
        positions.append(_build_fx_position(
            position_id=position_id,
            deals=ordered,
            orders=grouped_orders.get(position_id, []),
            balance_after=close_balances.get(position_id, 0.0),
            leverage=leverage,
        ))

    per_lot = value_per_lot(positions)
    for position in positions:
        apply_rr(position, resolve_value_per_price_unit(position, per_lot))

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

    position_orders = build_orders(orders, deals, position_key) or build_orders_from_deals(deals, position_key)
    take_profit, stop_loss = _protection(orders)

    return fx.FXPosition(
        id=position_key,
        side=trade_side(first_open.type),
        pair=first_open.symbol or last_close.symbol,
        amount=round8(sum(deal.volume for deal in closing)),
        entry_price=weighted_price([(deal.volume, deal.price) for deal in opening]),
        exit_price=weighted_price([(deal.volume, deal.price) for deal in closing]),
        pnl=round8(pnl),
        net_pnl=round8(net),
        commission=commission,
        swap=round8(swap),
        tp=take_profit,
        sl=stop_loss,
        multiplier=leverage if leverage > 0 else 1,
        closed=True,
        status=fx.STATUS_WIN if net > 0 else fx.STATUS_LOSE,
        created_at=first_open.time,
        closed_at=last_close.time,
        orders=position_orders,
        balance_init=round8(balance_after - net),
    )


def _protection(orders: list[raw.RawOrder]) -> tuple[float | None, float | None]:
    take_profit: float | None = None
    stop_loss: float | None = None
    for order in sorted(orders, key=lambda order: (order.time_setup is None, order.time_setup)):
        if take_profit is None and order.tp:
            take_profit = order.tp
        if stop_loss is None and order.sl:
            stop_loss = order.sl
    return take_profit, stop_loss
