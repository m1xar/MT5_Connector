from __future__ import annotations

import time
from typing import Any, Iterable

from utils.logging import get_logger, log_event

logger = get_logger(__name__)

# Tried in order until one of them quotes. A broker that carries none of these
# is possible, so the account's own traded symbols are tried first.
_PROBE_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD")

# No real trade server is further than this from UTC. The bound doubles as the
# staleness check: quotes stop at the weekend, and a tick left over from Friday
# reads as an offset of tens of hours, which lands outside it and is discarded.
_MAX_OFFSET_MINUTES = 14 * 60

# Trade servers sit on whole or half hours. Rounding turns "2h 59m 58s of tick
# age and network lag" into a clean 3h.
_ROUND_TO_MINUTES = 30


def measure_server_offset(
    terminal: Any,
    symbols: Iterable[str] = (),
) -> int | None:
    """Minutes the trade server's clock runs ahead of real UTC, or None.

    MT5 has no call for "what timezone is the server on", and every timestamp
    it hands out - deal times, order times, bar times - is stamped in that
    clock while looking exactly like a UTC epoch. What it does have is a tick,
    stamped in the same clock: comparing it against real UTC recovers the
    offset.

    The measurement is only meaningful while the market is quoting. Over a
    weekend the last tick is days old and the difference is nonsense, so
    anything beyond what a real trade server could be is reported as unknown
    rather than believed.
    """
    candidates: list[str] = []
    for symbol in (*symbols, *_PROBE_SYMBOLS):
        if symbol and symbol not in candidates:
            candidates.append(symbol)

    for symbol in candidates:
        try:
            terminal.mt5.symbol_select(symbol, True)
            tick = terminal.mt5.symbol_info_tick(symbol)
        except Exception:
            continue
        if tick is None:
            continue

        server_seconds = getattr(tick, "time", 0) or 0
        if not server_seconds:
            continue

        drift_minutes = (int(server_seconds) - time.time()) / 60.0
        if abs(drift_minutes) > _MAX_OFFSET_MINUTES:
            # Stale quote, not a real offset.
            continue

        offset = int(round(drift_minutes / _ROUND_TO_MINUTES) * _ROUND_TO_MINUTES)
        log_event(
            logger,
            "info",
            "terminal.clock.measured",
            symbol=symbol,
            offset_minutes=offset,
            raw_drift_minutes=round(drift_minutes, 2),
        )
        return offset

    log_event(logger, "warning", "terminal.clock.unknown", tried=len(candidates))
    return None
