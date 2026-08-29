from __future__ import annotations

from domain import fx

from .. import raw
from ..helpers.mathutil import abs8, round8


def trade_side(deal_type: int) -> str:
    return fx.SIDE_LONG if deal_type == raw.DEAL_TYPE_BUY else fx.SIDE_SHORT


def execution_side(deal_type: int) -> str:
    return fx.EXEC_SIDE_BUY if deal_type == raw.DEAL_TYPE_BUY else fx.EXEC_SIDE_SELL


def order_execution_side(order_type: int) -> str:
    return fx.EXEC_SIDE_BUY if order_type % 2 == 0 else fx.EXEC_SIDE_SELL


def order_type(order: raw.RawOrder | None) -> str:
    if order is None:
        return fx.ORDER_TYPE_MARKET
    if order.type in (raw.ORDER_TYPE_BUY, raw.ORDER_TYPE_SELL, raw.ORDER_TYPE_CLOSE_BY):
        return fx.ORDER_TYPE_MARKET
    if order.type in (raw.ORDER_TYPE_BUY_LIMIT, raw.ORDER_TYPE_SELL_LIMIT):
        return fx.ORDER_TYPE_LIMIT
    if order.type in (raw.ORDER_TYPE_BUY_STOP, raw.ORDER_TYPE_SELL_STOP):
        return fx.ORDER_TYPE_STOP
    if order.type in (raw.ORDER_TYPE_BUY_STOP_LIMIT, raw.ORDER_TYPE_SELL_STOP_LIMIT):
        return fx.ORDER_TYPE_STOP_LIMIT
    return fx.ORDER_TYPE_MARKET


def order_status(state: int) -> str:
    if state == raw.ORDER_STATE_FILLED:
        return fx.ORDER_STATUS_FILLED
    if state == raw.ORDER_STATE_REJECTED:
        return fx.ORDER_STATUS_REJECTED
    if state == raw.ORDER_STATE_EXPIRED:
        return fx.ORDER_STATUS_EXPIRED
    if state == raw.ORDER_STATE_CANCELED:
        return fx.ORDER_STATUS_CANCELLED
    return fx.ORDER_STATUS_ACCEPTED


def _filled_order_status(order: raw.RawOrder | None, deal: raw.RawDeal | None) -> str:
    if deal is not None:
        return fx.ORDER_STATUS_FILLED
    if order is None:
        return fx.ORDER_STATUS_FILLED
    if order.volume_initial > order.volume_current:
        return fx.ORDER_STATUS_FILLED
    return order_status(order.state)


def _stop_and_original_price(order: raw.RawOrder) -> tuple[float, float]:
    kind = order_type(order)
    if kind == fx.ORDER_TYPE_STOP_LIMIT:
        return order.price_open, order.price_stoplimit
    if kind == fx.ORDER_TYPE_STOP:
        return order.price_open, 0.0
    if kind == fx.ORDER_TYPE_LIMIT:
        return 0.0, order.price_open
    return 0.0, 0.0


def build_trade(deal: raw.RawDeal, order_id: str) -> fx.FXTrade:
    return fx.FXTrade(
        order_id=order_id,
        side=execution_side(deal.type),
        price=deal.price,
        amount=deal.volume,
        commission=abs8(deal.commission + deal.fee),
        profit=round8(deal.profit),
        done_at=deal.time,
    )


def build_orders(
    orders: list[raw.RawOrder],
    deals: list[raw.RawDeal],
    position_id: str,
) -> list[fx.FXOrder]:
    deal_by_order: dict[int, raw.RawDeal] = {}
    for deal in deals:
        if deal.order:
            deal_by_order[deal.order] = deal

    out: list[fx.FXOrder] = []
    for order in orders:
        deal = deal_by_order.get(order.ticket)
        order_id = str(order.ticket)
        stop_price, original_price = _stop_and_original_price(order)

        average_price = deal.price if deal is not None else order.price_open
        if deal is not None:
            amount_filled = deal.volume
        else:
            amount_filled = max(order.volume_initial - order.volume_current, 0.0)

        domain_order = fx.FXOrder(
            id=order_id,
            position_id=position_id,
            type=order_type(order),
            status=_filled_order_status(order, deal),
            side=execution_side(deal.type) if deal is not None else order_execution_side(order.type),
            amount=order.volume_initial,
            amount_filled=amount_filled,
            average_price=average_price,
            stop_price=stop_price,
            original_price=original_price,
            updated_at=order.time_done or order.time_setup,
        )
        if deal is not None:
            domain_order.trade = build_trade(deal, order_id)
        out.append(domain_order)
    return out


def build_orders_from_deals(deals: list[raw.RawDeal], position_id: str) -> list[fx.FXOrder]:
    out: list[fx.FXOrder] = []
    for deal in deals:
        order_id = str(deal.order or deal.ticket)
        out.append(
            fx.FXOrder(
                id=order_id,
                position_id=position_id,
                type=fx.ORDER_TYPE_MARKET,
                status=fx.ORDER_STATUS_FILLED,
                side=execution_side(deal.type),
                amount=deal.volume,
                amount_filled=deal.volume,
                average_price=deal.price,
                updated_at=deal.time,
                trade=build_trade(deal, order_id),
            )
        )
    return out
