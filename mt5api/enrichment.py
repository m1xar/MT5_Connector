from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Collection, Iterable, Mapping

from domain import fx
from utils.logging import log_event

from .numbers import round8
from .raw import RawCandle

logger = logging.getLogger(__name__)

CandleFetcher = Callable[[str, str, datetime, datetime], list[RawCandle]]

MINUTE = "1m"
DAY = "1d"

_MIN_SPAN_FOR_DAILY = timedelta(days=2)
_TICK = timedelta(microseconds=1)
_OVERLAP_TOLERANCE = 0.01


def value_per_price_unit(position: fx.FXPosition) -> float | None:
    price_delta = position.exit_price - position.entry_price
    if position.side == fx.SIDE_SHORT:
        price_delta = -price_delta
    if price_delta != 0 and position.pnl != 0:
        return abs(position.pnl / price_delta)
    return None


def value_per_lot(positions: Iterable[fx.FXPosition]) -> dict[str, float]:
    rates: dict[str, float] = {}
    for position in positions:
        if not position.pair or position.pair in rates or not position.amount:
            continue
        unit = value_per_price_unit(position)
        if unit is not None:
            rates[position.pair] = unit / position.amount
    return rates


def resolve_value_per_price_unit(
    position: fx.FXPosition, per_lot: Mapping[str, float] | None = None
) -> float | None:
    unit = value_per_price_unit(position)
    if unit is not None:
        return unit
    rate = (per_lot or {}).get(position.pair)
    if rate is None or not position.amount:
        return None
    return rate * position.amount


def apply_rr(position: fx.FXPosition, unit: float | None) -> None:
    if not position.sl or not position.entry_price:
        return
    risk_price = abs(position.entry_price - position.sl)
    if risk_price == 0:
        return
    if position.tp:
        position.rr_planned = round8(abs(position.tp - position.entry_price) / risk_price)
    if unit is None:
        return
    risk_money = risk_price * unit
    if risk_money > 0:
        position.rr = round8(position.net_pnl / risk_money)


def apply_mae_mfe(position: fx.FXPosition, high: float, low: float, unit: float | None) -> bool:
    if unit is None:
        return False
    traded_low = min(position.entry_price, position.exit_price)
    traded_high = max(position.entry_price, position.exit_price)
    slack = traded_high * _OVERLAP_TOLERANCE
    if low > traded_high + slack or high < traded_low - slack:
        return False
    high = max(high, traded_high)
    low = min(low, traded_low)
    if position.side == fx.SIDE_LONG:
        position.mae = round8(min(0.0, (low - position.entry_price) * unit))
        position.mfe = round8(max(0.0, (high - position.entry_price) * unit))
    else:
        position.mae = round8(min(0.0, (position.entry_price - high) * unit))
        position.mfe = round8(max(0.0, (position.entry_price - low) * unit))
    return True


def _segments(start: datetime, end: datetime) -> list[tuple[str, datetime, datetime]]:
    if end <= start or end - start < _MIN_SPAN_FOR_DAILY:
        return [(MINUTE, start, end)]
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
    first_day = midnight if midnight == start else midnight + timedelta(days=1)
    last_day = end.replace(hour=0, minute=0, second=0, microsecond=0)
    if last_day <= first_day:
        return [(MINUTE, start, end)]
    segments: list[tuple[str, datetime, datetime]] = []
    if start < first_day:
        segments.append((MINUTE, start, first_day - _TICK))
    segments.append((DAY, first_day, last_day - _TICK))
    segments.append((MINUTE, last_day, end))
    return segments


def enrich_mae_mfe(
    positions: Iterable[fx.FXPosition], fetch: CandleFetcher, already_measured: Collection[str] = ()
) -> int:
    everything = list(positions)
    measured = set(already_measured)
    closed = [
        p for p in everything
        if p.closed_at is not None and p.created_at is not None and p.pair and p.id not in measured
    ]
    if not closed:
        log_event(logger, "info", "enrich.skipped", already_measured=len(measured))
        return 0

    per_lot = value_per_lot(everything)
    enriched = rejected = 0
    for position in closed:
        try:
            candles: list[RawCandle] = []
            for interval, start, end in _segments(position.created_at, position.closed_at):
                candles.extend(fetch(position.pair, interval, start, end))
        except Exception as exc:
            log_event(
                logger, "warning", "enrich.candles.failed",
                pair=position.pair, position_id=position.id, error=str(exc),
            )
            continue
        if not candles:
            continue
        high = max(c.high for c in candles)
        low = min(c.low for c in candles)
        if apply_mae_mfe(position, high, low, resolve_value_per_price_unit(position, per_lot)):
            enriched += 1
        else:
            rejected += 1

    log_event(
        logger, "info", "enrich.completed",
        positions=len(closed), enriched=enriched, rejected=rejected, already_measured=len(measured),
    )
    return enriched
