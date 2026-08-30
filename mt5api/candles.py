from __future__ import annotations

from datetime import datetime, timedelta

from utils.logging import get_logger, log_event

from .helpers.timeutil import as_naive_utc
from .raw import TIMEFRAME_D1, TIMEFRAME_M1, RawCandle
from .terminal import MT5Terminal

logger = get_logger(__name__)


_TIMEFRAMES = {"1m": TIMEFRAME_M1, "1d": TIMEFRAME_D1}

# Wider than any real trade server is from UTC, so the requested window is
# always inside what comes back whichever way the server's clock leans.
_REQUEST_SLACK = timedelta(hours=15)


def fetch_candles(
    terminal: MT5Terminal,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> list[RawCandle]:
    """Candles inside [start, end], and nothing else.

    Two things make this less obvious than it looks.

    `copy_rates_range` reads the datetimes it is given in the *trade server's*
    clock, while the bar times it returns line up with deal times. Against a
    UTC+3 server, asking for 12:00-14:00 hands back bars stamped 09:00-11:00.
    Filtering that to the requested window leaves only the overlap, which is
    empty for any position shorter than the offset - 61 of one account's 91
    positions came back with no candles at all for exactly that reason. Rather
    than calibrate an offset per broker (and re-calibrate it twice a year for
    daylight saving), the request is widened by more than any real offset and
    the answer trimmed by bar time, which has been verified against deal times:
    the bar holding a deal's entry price sits within a minute of the deal.

    And `copy_rates_range` does not report "nothing for that range" as an empty
    result. Asked for three hours in 2022 on a symbol whose minute history only
    reaches back ~70 days, it returns a single unrelated bar from months later.
    That once produced a 161 EUR excursion on a 0.01 lot position in a 109 EUR
    account. Dropping everything is the right answer when the history is not
    there: `candle_high_low` then yields no high or low, and MAE/MFE stay None
    instead of becoming confident nonsense.
    """
    timeframe = _TIMEFRAMES[interval]
    terminal.mt5.symbol_select(symbol, True)
    window_start, window_end = as_naive_utc(start), as_naive_utc(end)
    rows = terminal.check_call(
        terminal.mt5.copy_rates_range(
            symbol,
            timeframe,
            window_start - _REQUEST_SLACK,
            window_end + _REQUEST_SLACK,
        ),
        "copy_rates_range",
    )

    candles = [RawCandle.from_mt5(row) for row in rows]
    inside = [
        candle
        for candle in candles
        if candle.time is not None
        and window_start <= as_naive_utc(candle.time) <= window_end
    ]
    if len(inside) != len(candles):
        log_event(
            logger,
            "warning" if not inside else "debug",
            "terminal.candles.out_of_range",
            symbol=symbol,
            interval=interval,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            returned=len(candles),
            kept=len(inside),
        )
    return inside
