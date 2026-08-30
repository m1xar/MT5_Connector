from __future__ import annotations

from datetime import datetime

from utils.logging import get_logger, log_event

from .helpers.timeutil import as_mt5_time, as_server_time
from .raw import TIMEFRAME_D1, TIMEFRAME_M1, RawCandle
from .terminal import MT5Terminal

logger = get_logger(__name__)


_TIMEFRAMES = {"1m": TIMEFRAME_M1, "1d": TIMEFRAME_D1}


def fetch_candles(
    terminal: MT5Terminal,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> list[RawCandle]:
    """Candles inside [start, end], and nothing else.

    Both ends are server-clock timestamps, which is what `copy_rates_range`
    matches its arguments against once `as_mt5_time` has kept them from being
    reinterpreted on the way in. The window then comes back exactly as asked
    for; it used to be widened by the server's offset and trimmed back, which
    was a fix for the wrong thing.

    The trimming stays, for a different reason. `copy_rates_range` does not
    report "nothing for that range" as an empty result: asked for three hours
    in 2022 on a symbol whose minute history only reaches back ~70 days, it
    returns a single unrelated bar from months later. That once produced a
    161 EUR excursion on a 0.01 lot position in a 109 EUR account. Dropping
    everything is the right answer when the history is not there -
    `candle_high_low` then yields no high or low, and MAE/MFE stay None
    instead of becoming confident nonsense.
    """
    timeframe = _TIMEFRAMES[interval]
    terminal.mt5.symbol_select(symbol, True)
    rows = terminal.check_call(
        terminal.mt5.copy_rates_range(
            symbol, timeframe, as_mt5_time(start), as_mt5_time(end)
        ),
        "copy_rates_range",
    )

    window_start, window_end = as_server_time(start), as_server_time(end)
    candles = [RawCandle.from_mt5(row) for row in rows]
    inside = [
        candle
        for candle in candles
        if candle.time is not None and window_start <= candle.time <= window_end
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
