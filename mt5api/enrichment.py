from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Collection, Iterable

from domain import fx
from utils.logging import get_logger, log_event

from .builders.enrich import apply_fx_mae_mfe, candle_high_low
from .helpers.candlespan import split
from .raw import RawCandle

logger = get_logger(__name__)

CandleFetcher = Callable[[str, str, datetime, datetime], list[RawCandle]]


def _from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)


def candles_for_span(
    fetch: CandleFetcher,
    pair: str,
    start: datetime,
    end: datetime,
) -> list[RawCandle]:
    candles: list[RawCandle] = []
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    for segment in split(start_ms, end_ms):
        candles.extend(
            fetch(pair, segment.interval, _from_ms(segment.start_ms), _from_ms(segment.end_ms))
        )
    return candles


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
            candles = candles_for_span(
                fetch, position.pair, position.created_at, position.closed_at
            )
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
