from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from utils.logging import get_logger, log_event

from .helpers.timeutil import to_real_utc

logger = get_logger(__name__)

UTC = datetime.timezone.utc

# The forex week opens and closes at 17:00 in New York, and has done since the
# market went electronic. New York's own daylight saving is in the zone data,
# so this is a known instant in real UTC for any date - which is the point: an
# anchor that does not come from the terminal.
_NEW_YORK = ZoneInfo("America/New_York")
_BOUNDARY_HOUR = 17
_FRIDAY, _SUNDAY = 4, 6

# Tried in order until one has enough history. The account's own symbols come
# first, since a broker need not carry any particular major.
_PROBE_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "EURUSD_i", "XAUUSD")

# About two months of minute bars, which is a dozen weekends - far more than
# the three readings this needs, and still under a second once cached.
_SCAN_BARS = 60000
_MIN_GAP_HOURS = 24
_MIN_SAMPLES = 3

# No real trade server is further than this from UTC; anything beyond came from
# a gap that was not a weekend.
_MAX_OFFSET_MINUTES = 14 * 60
# Trade servers sit on whole or half hours. Brokers stop quoting a few minutes
# before the nominal close and resume a few after the open - measured at 3 and
# 6 minutes on two brokers - and rounding absorbs that.
_ROUND_TO_MINUTES = 30


@dataclass(frozen=True)
class ServerClock:
    """How far the trade server's clock runs ahead of real UTC."""

    offset_minutes: int | None = None

    @property
    def known(self) -> bool:
        return self.offset_minutes is not None

    def to_utc(self, value: datetime.datetime | None) -> datetime.datetime | None:
        return to_real_utc(value, self.offset_minutes)


def _boundary_utc(label: datetime.datetime, weekday: int) -> datetime.datetime | None:
    """Real UTC of the 17:00 New York boundary nearest this server label."""
    best = None
    for delta in range(-2, 3):
        day = label.date() + datetime.timedelta(days=delta)
        if day.weekday() != weekday:
            continue
        boundary = (
            datetime.datetime.combine(day, datetime.time(_BOUNDARY_HOUR), tzinfo=_NEW_YORK)
            .astimezone(UTC)
            .replace(tzinfo=None)
        )
        if best is None or abs(label - boundary) < abs(label - best):
            best = boundary
    return best


def _readings(bars: Any) -> list[float]:
    """One offset reading per weekend edge, in minutes.

    The bar before a gap is the Friday close and the one after it is the Sunday
    open. Reading both cancels out a broker that stops quoting early and
    resumes late.
    """
    times = [int(row["time"]) for row in bars]
    readings: list[float] = []

    for before, after in zip(times, times[1:]):
        if after - before < _MIN_GAP_HOURS * 3600:
            continue
        for epoch, weekday in ((before, _FRIDAY), (after, _SUNDAY)):
            label = datetime.datetime.fromtimestamp(epoch, UTC).replace(tzinfo=None)
            boundary = _boundary_utc(label, weekday)
            if boundary is None:
                continue
            minutes = (label - boundary).total_seconds() / 60
            if abs(minutes) <= _MAX_OFFSET_MINUTES:
                readings.append(minutes)
    return readings


def measure_server_clock(terminal: Any, symbols: Iterable[str] = ()) -> ServerClock:
    """The server's offset from real UTC, read off the trading week.

    MT5 has no call for "what timezone is the server on". Every timestamp it
    hands out is stamped in that clock while looking exactly like a UTC epoch,
    and `terminal_info` and `account_info` carry no time at all. The only clock
    the API exposes outside history is the last quote's, which is the wrong
    thing to measure against: quotes stop at the weekend, and a tick left over
    from Friday reads as a plausible offset for the next fourteen hours rather
    than an obvious error - drifting an hour further out with every hour that
    passes, and believed the whole way.

    The trading week is the anchor instead. It opens and closes at 17:00 in New
    York, a known instant in real UTC for any date, and shows up in the bar
    labels as the edges of the weekend gap. The difference is the offset. Bars
    are pulled by position, so no date is ever sent to the terminal and nothing
    can be shifted on the way in, and several weekends are reduced by median so
    one holiday cannot decide it. Measured on two brokers, eight weekends each:
    every reading agreed, with the market shut.
    """
    candidates: list[str] = []
    for symbol in (*symbols, *_PROBE_SYMBOLS):
        if symbol and symbol not in candidates:
            candidates.append(symbol)

    for symbol in candidates:
        try:
            terminal.mt5.symbol_select(symbol, True)
            bars = terminal.mt5.copy_rates_from_pos(
                symbol, terminal.mt5.TIMEFRAME_M1, 0, _SCAN_BARS
            )
        except Exception:
            continue
        if bars is None or len(bars) < 2:
            continue

        readings = _readings(bars)
        if len(readings) < _MIN_SAMPLES:
            continue

        readings.sort()
        median = readings[len(readings) // 2]
        offset = int(round(median / _ROUND_TO_MINUTES) * _ROUND_TO_MINUTES)
        log_event(
            logger,
            "info",
            "terminal.clock.measured",
            symbol=symbol,
            offset_minutes=offset,
            samples=len(readings),
            spread_minutes=round(readings[-1] - readings[0], 1),
        )
        return ServerClock(offset_minutes=offset)

    log_event(logger, "warning", "terminal.clock.unknown", tried=len(candidates))
    return ServerClock()
