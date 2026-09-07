from __future__ import annotations

import datetime
import logging
import time
from dataclasses import dataclass
from typing import Any, Collection, Iterable, NamedTuple, Sequence
from zoneinfo import ZoneInfo

from utils.logging import log_event

from .timeutil import as_server_time, from_epoch

logger = logging.getLogger(__name__)

UTC = datetime.timezone.utc

_NEW_YORK = ZoneInfo("America/New_York")
_CLOSE_HOUR = 17
_FRIDAY = 4

_PROBE_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "EURUSD_i", "XAUUSD")

_SCAN_BARS = 6000
_MIN_GAP_HOURS = 24
_MIN_WEEKENDS = 3
_SYMBOL_WAIT_SECONDS = 30.0
_SYMBOL_POLL_SECONDS = 5.0

_MAX_OFFSET_MINUTES = 14 * 60
_ROUND_TO_MINUTES = 30
_MAX_SWITCH_MINUTES = 60
_CONFIRMATIONS = 2


class _Weekend(NamedTuple):
    closed_at: datetime.datetime
    opens_at: datetime.datetime
    offset_minutes: int


class _Scan(NamedTuple):
    clock: ServerClock | None
    weekends: int
    continuous: bool


def _new_york_close(day: datetime.date) -> datetime.datetime:
    return (
        datetime.datetime.combine(day, datetime.time(_CLOSE_HOUR), tzinfo=_NEW_YORK)
        .astimezone(UTC)
        .replace(tzinfo=None)
    )


@dataclass(frozen=True)
class ServerClock:
    steps: tuple[tuple[datetime.datetime, int], ...] = ()
    scanned: bool = False

    @classmethod
    def from_rows(cls, rows: Sequence[Any] | None) -> ServerClock:
        steps = []
        for at, minutes in rows or ():
            if isinstance(at, str):
                at = datetime.datetime.fromisoformat(at)
            steps.append((as_server_time(at), int(minutes)))
        steps.sort()
        return cls(steps=tuple(steps))

    @property
    def rows(self) -> list[list[Any]]:
        return [[at.isoformat(), minutes] for at, minutes in self.steps]

    @property
    def known(self) -> bool:
        return bool(self.steps)

    @property
    def offset_minutes(self) -> int | None:
        return self.steps[-1][1] if self.steps else None

    def offset_at(self, value: datetime.datetime | None) -> int | None:
        if value is None:
            return None
        label = as_server_time(value)
        for at, minutes in reversed(self.steps):
            if label >= at:
                return minutes
        return None

    def to_utc(self, value: datetime.datetime | None) -> datetime.datetime | None:
        minutes = self.offset_at(value)
        if value is None or minutes is None:
            return None
        return (as_server_time(value) - datetime.timedelta(minutes=minutes)).replace(tzinfo=UTC)

    def to_server(self, value: datetime.datetime | None) -> datetime.datetime | None:
        if value is None:
            return None
        instant = as_server_time(value.astimezone(UTC)) if value.tzinfo is not None else value
        for at, minutes in reversed(self.steps):
            if instant + datetime.timedelta(minutes=minutes) >= at:
                return value + datetime.timedelta(minutes=minutes)
        return None


def _week_bounds_utc(now: datetime.datetime) -> tuple[datetime.datetime, datetime.datetime]:
    close_day = now.date()
    while close_day.weekday() != _FRIDAY:
        close_day += datetime.timedelta(days=1)
    close = _new_york_close(close_day)
    if close < now:
        close += datetime.timedelta(days=7)
    return close - datetime.timedelta(days=5), close


def _market_is_open(now: datetime.datetime) -> bool:
    open_, close = _week_bounds_utc(now)
    return open_ <= now <= close


def _current_weekfrom_epoch(offset_minutes: int) -> datetime.datetime:
    now = datetime.datetime.now(UTC).replace(tzinfo=None)
    week_open, _ = _week_bounds_utc(now)
    if week_open > now:
        week_open -= datetime.timedelta(days=7)
    return week_open + datetime.timedelta(minutes=offset_minutes)


def _offset_from_tick(
    terminal: Any, symbols: Iterable[str], *, continuous: Collection[str] = ()
) -> int | None:
    now = datetime.datetime.now(UTC).replace(tzinfo=None)
    open_now = _market_is_open(now)
    for symbol in symbols:
        if not symbol or (not open_now and symbol not in continuous):
            continue
        try:
            terminal.mt5.symbol_select(symbol, True)
            tick = terminal.mt5.symbol_info_tick(symbol)
        except Exception:
            continue
        if tick is None or not getattr(tick, "time", 0):
            continue
        minutes = (from_epoch(tick.time) - now).total_seconds() / 60.0
        if abs(minutes) > _MAX_OFFSET_MINUTES:
            continue
        return int(round(minutes / 60.0) * 60)
    return None


def _friday_close_utc(label: datetime.datetime) -> datetime.datetime | None:
    best = None
    for delta in range(-2, 3):
        day = label.date() + datetime.timedelta(days=delta)
        if day.weekday() != _FRIDAY:
            continue
        close = _new_york_close(day)
        if best is None or abs(label - close) < abs(label - best):
            best = close
    return best


def _weekends(bars: Any) -> list[_Weekend]:
    times = [int(row["time"]) for row in bars]
    readings: list[_Weekend] = []
    for before, after in zip(times, times[1:]):
        if after - before < _MIN_GAP_HOURS * 3600:
            continue
        closed_at = from_epoch(before) + datetime.timedelta(hours=1)
        close = _friday_close_utc(closed_at)
        if close is None:
            continue
        minutes = (closed_at - close).total_seconds() / 60
        if abs(minutes) > _MAX_OFFSET_MINUTES:
            continue
        readings.append(_Weekend(
            closed_at=closed_at,
            opens_at=from_epoch(after),
            offset_minutes=int(round(minutes / _ROUND_TO_MINUTES) * _ROUND_TO_MINUTES),
        ))
    return readings


def _steps(
    from_label: datetime.datetime, weekends: Sequence[_Weekend]
) -> tuple[tuple[datetime.datetime, int], ...]:
    steps: list[tuple[datetime.datetime, int]] = []
    for index, weekend in enumerate(weekends):
        confirmations = weekends[index + 1: index + 1 + _CONFIRMATIONS]
        if len(confirmations) < _CONFIRMATIONS:
            continue
        if any(later.offset_minutes != weekend.offset_minutes for later in confirmations):
            continue
        if not steps:
            steps.append((from_label, weekend.offset_minutes))
            continue
        current = steps[-1][1]
        if weekend.offset_minutes == current or abs(weekend.offset_minutes - current) > _MAX_SWITCH_MINUTES:
            continue
        steps.append((weekends[index - 1].opens_at, weekend.offset_minutes))
    return tuple(steps)


def _scan_symbol(terminal: Any, symbol: str) -> _Scan:
    deadline = time.monotonic() + _SYMBOL_WAIT_SECONDS
    while True:
        try:
            if not terminal.mt5.symbol_select(symbol, True):
                return _Scan(None, 0, continuous=False)
            bars = terminal.mt5.copy_rates_from_pos(symbol, terminal.mt5.TIMEFRAME_H1, 0, _SCAN_BARS)
        except Exception:
            bars = None

        if bars is not None and len(bars) >= 2:
            weekends = _weekends(bars)
            if len(weekends) >= _MIN_WEEKENDS:
                clock = ServerClock(steps=_steps(from_epoch(bars[0]["time"]), weekends), scanned=True)
                if clock.known:
                    return _Scan(clock, len(weekends), continuous=False)
            if len(bars) >= _SCAN_BARS:
                return _Scan(None, 0, continuous=not weekends)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _Scan(None, 0, continuous=False)
        time.sleep(min(_SYMBOL_POLL_SECONDS, remaining))


def measure_server_clock(terminal: Any, symbols: Iterable[str] = ()) -> ServerClock:
    candidates: list[str] = []
    for symbol in (*symbols, *_PROBE_SYMBOLS):
        if symbol and symbol not in candidates:
            candidates.append(symbol)

    continuous: list[str] = []
    scanned: ServerClock | None = None
    scanned_symbol, scanned_weekends, scanned_waited = "", 0, 0.0

    for symbol in candidates:
        started = time.monotonic()
        scan = _scan_symbol(terminal, symbol)
        waited = time.monotonic() - started
        if scan.continuous:
            continuous.append(symbol)
        if scan.clock is None:
            log_event(
                logger, "info", "terminal.clock.symbol.rejected",
                symbol=symbol, continuous=scan.continuous, waited_seconds=round(waited, 1),
            )
            continue
        scanned, scanned_symbol, scanned_weekends, scanned_waited = scan.clock, symbol, scan.weekends, waited
        break

    live = _offset_from_tick(terminal, candidates, continuous=continuous)

    if scanned is not None:
        clock = scanned
        if live is not None and live != clock.offset_minutes:
            clock = ServerClock(steps=clock.steps + ((_current_weekfrom_epoch(live), live),), scanned=True)
            log_event(
                logger, "info", "terminal.clock.tick.pinned",
                symbol=scanned_symbol, from_bars=clock.steps[-2][1], from_tick=live,
                since=clock.steps[-1][0].isoformat(),
            )
        log_event(
            logger, "info", "terminal.clock.measured",
            symbol=scanned_symbol, offset_minutes=clock.offset_minutes, weekends=scanned_weekends,
            switches=len(clock.steps) - 1, measured_from=clock.steps[0][0].isoformat(),
            waited_seconds=round(scanned_waited, 1),
        )
        return clock

    if live is not None:
        clock = ServerClock(steps=((_current_weekfrom_epoch(live), live),))
        log_event(
            logger, "warning", "terminal.clock.from_tick",
            offset_minutes=live, valid_from=clock.steps[0][0].isoformat(), tried=len(candidates),
        )
        return clock

    log_event(logger, "warning", "terminal.clock.unknown", tried=len(candidates))
    return ServerClock()
