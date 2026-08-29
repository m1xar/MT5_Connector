from __future__ import annotations

from domain import fx

from ..raw import RawCandle
from ..helpers.mathutil import round8


def value_per_price_unit(position: fx.FXPosition) -> float:
    price_delta = position.exit_price - position.entry_price
    if position.side == fx.SIDE_SHORT:
        price_delta = position.entry_price - position.exit_price
    if price_delta != 0 and position.pnl != 0:
        return abs(position.pnl / price_delta)
    return position.amount


def candle_high_low(candles: list[RawCandle]) -> tuple[float | None, float | None]:
    if not candles:
        return None, None
    high = candles[0].high
    low = candles[0].low
    for candle in candles[1:]:
        if candle.high > high:
            high = candle.high
        if candle.low < low:
            low = candle.low
    return high, low


def apply_fx_mae_mfe(
    position: fx.FXPosition,
    high: float | None,
    low: float | None,
) -> None:
    if high is None or low is None:
        return
    unit = value_per_price_unit(position)
    if position.side == fx.SIDE_LONG:
        position.mae = round8(min(0.0, (low - position.entry_price) * unit))
        position.mfe = round8(max(0.0, (high - position.entry_price) * unit))
        return
    position.mae = round8(min(0.0, (position.entry_price - high) * unit))
    position.mfe = round8(max(0.0, (position.entry_price - low) * unit))


def apply_rr(position: fx.FXPosition) -> None:
    if not position.sl or not position.entry_price:
        return
    risk_price = abs(position.entry_price - position.sl)
    if risk_price == 0:
        return

    risk_money = risk_price * value_per_price_unit(position)
    if risk_money > 0:
        position.rr = round8(position.net_pnl / risk_money)

    if position.tp:
        position.rr_planned = round8(abs(position.tp - position.entry_price) / risk_price)
