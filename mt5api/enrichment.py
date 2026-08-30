from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Collection, Iterable

from domain import fx
from utils.logging import get_logger, log_event

from .builders.enrich import apply_fx_mae_mfe, candle_high_low
from .raw import RawCandle

logger = get_logger(__name__)

CandleFetcher = Callable[[str, str, datetime, datetime], list[RawCandle]]

MINUTE = "1m"
DAY = "1d"

# Under this, a position is priced from minute bars end to end. Over it, the
# whole days in the middle come from daily bars instead: a fortnight of minute
# bars is twenty thousand rows to find one high and one low.
_MIN_SPAN_FOR_DAILY = timedelta(days=2)
_TICK = timedelta(microseconds=1)


def _segments(start: datetime, end: datetime) -> list[tuple[str, datetime, datetime]]:
    """The window split into the coarsest bars that still cover it exactly.

    Both ends are server-clock timestamps, and a daily bar is stamped at the
    server's own midnight, so the day boundaries are taken from the timestamps
    as they stand rather than converted first.
    """
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
    positions: Iterable[fx.FXPosition],
    fetch: CandleFetcher,
    already_measured: Collection[str] = (),
) -> int:
    """Fill in MAE/MFE for closed positions that do not have it yet.

    A sync always rebuilds the whole history - the ledger needs every deal
    to recover BalanceInit - but a closed position's excursion never changes
    once measured. Re-measuring it means asking the terminal for candles
    from years ago on every run, and the terminal can only serve those by
    holding a contiguous minute series back to that date: one three-hour
    window in 2022 cost 170 MB of cache. So positions already carrying a
    figure are left alone, and only genuinely new ones are priced.
    """
    measured = set(already_measured)
    closed = [
        position
        for position in positions
        if position.closed_at is not None
        and position.created_at is not None
        and position.pair
        and position.id not in measured
    ]
    if not closed:
        log_event(logger, "info", "enrich.skipped", already_measured=len(measured))
        return 0

    enriched = 0
    rejected = 0
    for position in closed:
        try:
            candles: list[RawCandle] = []
            for interval, start, end in _segments(
                position.created_at, position.closed_at
            ):
                candles.extend(fetch(position.pair, interval, start, end))
        except Exception as exc:
            log_event(
                logger,
                "warning",
                "enrich.candles.failed",
                pair=position.pair,
                position_id=position.id,
                error=str(exc),
            )
            continue
        high, low = candle_high_low(candles)
        if high is None:
            continue
        if apply_fx_mae_mfe(position, high, low):
            enriched += 1
        else:
            # Candles came back, but not enough of the window for the
            # figures to bracket the realised result. Left unmeasured so a
            # later sync can try again once the history has filled in.
            rejected += 1

    log_event(
        logger,
        "info",
        "enrich.completed",
        positions=len(closed),
        enriched=enriched,
        rejected=rejected,
        already_measured=len(measured),
    )
    return enriched
