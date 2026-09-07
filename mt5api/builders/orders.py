from __future__ import annotations

from domain import fx

from .. import raw
from ..numbers import abs8, round8

_ORDER_TYPES = {
    raw.ORDER_TYPE_BUY: fx.ORDER_TYPE_MARKET,
    raw.ORDER_TYPE_SELL: fx.ORDER_TYPE_MARKET,
    raw.ORDER_TYPE_CLOSE_BY: fx.ORDER_TYPE_MARKET,
    raw.ORDER_TYPE_BUY_LIMIT: fx.ORDER_TYPE_LIMIT,
    raw.ORDER_TYPE_SELL_LIMIT: fx.ORDER_TYPE_LIMIT,
    raw.ORDER_TYPE_BUY_STOP: fx.ORDER_TYPE_STOP,
    raw.ORDER_TYPE_SELL_STOP: fx.ORDER_TYPE_STOP,
    raw.ORDER_TYPE_BUY_STOP_LIMIT: fx.ORDER_TYPE_STOP_LIMIT,
    raw.ORDER_TYPE_SELL_STOP_LIMIT: fx.ORDER_TYPE_STOP_LIMIT,
}

_ORDER_STATES = {
    raw.ORDER_STATE_FILLED: fx.ORDER_STATUS_FILLED,
    raw.ORDER_STATE_REJECTED: fx.ORDER_STATUS_REJECTED,
    raw.ORDER_STATE_EXPIRED: fx.ORDER_STATUS_EXPIRED,
    raw.ORDER_STATE_CANCELED: fx.ORDER_STATUS_CANCELLED,
}


def trade_side(deal_type: int) -> str:
    return fx.SIDE_LONG if deal_type == raw.DEAL_TYPE_BUY else fx.SIDE_SHORT


def _execution_side(deal_type: int) -> str:
    return fx.EXEC_SIDE_BUY if deal_type == raw.DEAL_TYPE_BUY else fx.EXEC_SIDE_SELL


def _order_status(order: raw.RawOrder, deal: raw.RawDeal | None) -> str:
    if deal is not None or order.volume_initial > order.volume_current:
        return fx.ORDER_STATUS_FILLED
    return _ORDER_STATES.get(order.state, fx.ORDER_STATUS_ACCEPTED)


def _prices(order: raw.RawOrder, kind: str) -> tuple[float, float]:
    if kind == fx.ORDER_TYPE_STOP_LIMIT:
        return order.price_open, order.price_stoplimit
    if kind == fx.ORDER_TYPE_STOP:
        return order.price_open, 0.0
    if kind == fx.ORDER_TYPE_LIMIT:
        return 0.0, order.price_open
    return 0.0, 0.0


def _trade(deal: raw.RawDeal, order_id: str) -> fx.FXTrade:
    return fx.FXTrade(
        order_id=order_id,
        side=_execution_side(deal.type),
        price=deal.price,
        amount=deal.volume,
        commission=abs8(deal.commission + deal.fee),
        profit=round8(deal.profit),
        done_at=deal.time,
    )


def build_orders(orders: list[raw.RawOrder], deals: list[raw.RawDeal], position_id: str) -> list[fx.FXOrder]:
    deal_by_order = {deal.order: deal for deal in deals if deal.order}
    built: list[fx.FXOrder] = []
    for order in orders:
        deal = deal_by_order.get(order.ticket)
        order_id = str(order.ticket)
        kind = _ORDER_TYPES.get(order.type, fx.ORDER_TYPE_MARKET)
        stop_price, original_price = _prices(order, kind)
        if deal is not None:
            side = _execution_side(deal.type)
        else:
            side = fx.EXEC_SIDE_BUY if order.type % 2 == 0 else fx.EXEC_SIDE_SELL
        built.append(fx.FXOrder(
            id=order_id,
            position_id=position_id,
            type=kind,
            status=_order_status(order, deal),
            side=side,
            amount=order.volume_initial,
            amount_filled=deal.volume if deal is not None else max(order.volume_initial - order.volume_current, 0.0),
            average_price=deal.price if deal is not None else order.price_open,
            stop_price=stop_price,
            original_price=original_price,
            updated_at=order.time_done or order.time_setup,
            trade=_trade(deal, order_id) if deal is not None else fx.FXTrade(),
        ))
    return built


def build_orders_from_deals(deals: list[raw.RawDeal], position_id: str) -> list[fx.FXOrder]:
    built: list[fx.FXOrder] = []
    for deal in deals:
        order_id = str(deal.order or deal.ticket)
        built.append(fx.FXOrder(
            id=order_id,
            position_id=position_id,
            type=fx.ORDER_TYPE_MARKET,
            status=fx.ORDER_STATUS_FILLED,
            side=_execution_side(deal.type),
            amount=deal.volume,
            amount_filled=deal.volume,
            average_price=deal.price,
            updated_at=deal.time,
            trade=_trade(deal, order_id),
        ))
    return built
