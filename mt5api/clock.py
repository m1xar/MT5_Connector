from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Iterable, NamedTuple, Sequence
from zoneinfo import ZoneInfo

from utils.logging import get_logger, log_event

logger = get_logger(__name__)

UTC = datetime.timezone.utc

# The forex week ends at 17:00 in New York, and has done since the market went
# electronic. New York's own daylight saving is in the zone data, so this is a
# known instant in real UTC for any date - which is the point: an anchor that
# does not come from the terminal.
_NEW_YORK = ZoneInfo("America/New_York")
_CLOSE_HOUR = 17
_FRIDAY = 4

# Tried in order until one has enough history. The account's own symbols come
# first, since a broker need not carry any particular major.
_PROBE_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "EURUSD_i", "XAUUSD")

# Hourly bars: ten years of them in one call, answered from the terminal's own
# cache in milliseconds. The same number of minute bars covers two months, and
# brokers only keep minute history for the recent past anyway - hourly reaches
# back further than any account this has to convert.
_SCAN_BARS = 60000
_MIN_GAP_HOURS = 24
_MIN_WEEKENDS = 3

# No real trade server is further than this from UTC; anything beyond came from
# a gap that was not a weekend.
_MAX_OFFSET_MINUTES = 14 * 60
# Trade servers sit on whole or half hours. Brokers stop quoting a few minutes
# before the nominal close - measured at 3 and 6 minutes on two brokers - and
# rounding absorbs that.
_ROUND_TO_MINUTES = 30
# Daylight saving moves a clock by an hour. A larger jump is a short holiday
# session, not a switch.
_MAX_SWITCH_MINUTES = 60


class _Weekend(NamedTuple):
    """One weekend gap in the bars, and what its Friday close reads."""

    closed_at: datetime.datetime
    opens_at: datetime.datetime
    offset_minutes: int


def _label(epoch: Any) -> datetime.datetime:
    """The server-clock label MT5 hands out dressed as a UTC epoch."""
    return datetime.datetime.fromtimestamp(int(epoch), UTC).replace(tzinfo=None)


def _naive(value: datetime.datetime) -> datetime.datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


@dataclass(frozen=True)
class ServerClock:
    """How far the trade server's clock ran ahead of real UTC, over time.

    One step per daylight saving switch, each keyed by the server label its
    offset applies from. A timestamp is therefore converted with the offset
    that was in force when it was stamped, not the one in force today - which
    is the whole difference between a position closed in July and the same
    position read back in January.
    """

    steps: tuple[tuple[datetime.datetime, int], ...] = ()

    @classmethod
    def from_rows(cls, rows: Sequence[Any] | None) -> "ServerClock":
        """Rebuild from what was stored on the account."""
        steps = []
        for row in rows or ():
            at, minutes = row
            if isinstance(at, str):
                at = datetime.datetime.fromisoformat(at)
            steps.append((_naive(at), int(minutes)))
        steps.sort()
        return cls(steps=tuple(steps))

    @property
    def rows(self) -> list[list[Any]]:
        """The steps as JSON, for storing on the account."""
        return [[at.isoformat(), minutes] for at, minutes in self.steps]

    @property
    def known(self) -> bool:
        return bool(self.steps)

    @property
    def offset_minutes(self) -> int | None:
        """The offset in force now - the last switch is the current one."""
        return self.steps[-1][1] if self.steps else None

    def offset_at(self, value: datetime.datetime | None) -> int | None:
        """The offset in force when this server-clock timestamp was stamped.

        None before the first step, which is as far back as the scan reached: a
        wrong answer here is worse than no answer.
        """
        if value is None:
            return None
        label = _naive(value)
        for at, minutes in reversed(self.steps):
            if label >= at:
                return minutes
        return None

    def to_utc(self, value: datetime.datetime | None) -> datetime.datetime | None:
        """The same instant with the server's offset taken off."""
        minutes = self.offset_at(value)
        if value is None or minutes is None:
            return None
        return value - datetime.timedelta(minutes=minutes)

    def to_server(self, value: datetime.datetime | None) -> datetime.datetime | None:
        """Real UTC expressed in the server's clock - the inverse of to_utc.

        Used to put a query window in the same clock as the timestamps it is
        compared against. Unknown reads as None here too, but callers fall back
        to the untranslated instant rather than dropping the window: being a
        few hours out at the edge of a days filter is not the same kind of
        wrong as publishing a *Utc field that is a lie.
        """
        if value is None:
            return None
        instant = _naive(value.astimezone(UTC)) if value.tzinfo is not None else value
        for at, minutes in reversed(self.steps):
            if instant + datetime.timedelta(minutes=minutes) >= at:
                return value + datetime.timedelta(minutes=minutes)
        return None


def _friday_close_utc(label: datetime.datetime) -> datetime.datetime | None:
    """Real UTC of the 17:00 New York close nearest this server label."""
    best = None
    for delta in range(-2, 3):
        day = label.date() + datetime.timedelta(days=delta)
        if day.weekday() != _FRIDAY:
            continue
        close = (
            datetime.datetime.combine(day, datetime.time(_CLOSE_HOUR), tzinfo=_NEW_YORK)
            .astimezone(UTC)
            .replace(tzinfo=None)
        )
        if best is None or abs(label - close) < abs(label - best):
            best = close
    return best


def _weekends(bars: Any) -> list[_Weekend]:
    """One reading per weekend, taken off the Friday that closes the week.

    Only the Friday edge is read. The Sunday one looks like it should say the
    same thing and does not: for the three weeks each March when New York has
    moved to summer time and Europe has not, this broker still opened its week
    at midnight server time - an hour after the market itself - and that edge
    reads an hour high for every one of them. The close follows the market.
    """
    times = [int(row["time"]) for row in bars]
    readings: list[_Weekend] = []

    for before, after in zip(times, times[1:]):
        if after - before < _MIN_GAP_HOURS * 3600:
            continue
        # An hourly bar is labelled with its open, so the week ends an hour
        # after the last one starts.
        closed_at = _label(before) + datetime.timedelta(hours=1)
        close = _friday_close_utc(closed_at)
        if close is None:
            continue
        minutes = (closed_at - close).total_seconds() / 60
        if abs(minutes) > _MAX_OFFSET_MINUTES:
            continue
        readings.append(
            _Weekend(
                closed_at=closed_at,
                opens_at=_label(after),
                offset_minutes=int(
                    round(minutes / _ROUND_TO_MINUTES) * _ROUND_TO_MINUTES
                ),
            )
        )
    return readings


def _steps(
    from_label: datetime.datetime, weekends: Sequence[_Weekend]
) -> tuple[tuple[datetime.datetime, int], ...]:
    """The weekend readings collapsed to the points where the offset changed.

    A reading can only come in low. The market really does stop at 17:00 in New
    York, so the last bar of the week cannot sit after it, while a broker that
    shuts early for a holiday leaves one sitting well before it - three and five
    hours early over Christmas, across ten years of one account's history. Two
    things tell a holiday from a daylight saving switch: a switch moves the
    clock by exactly an hour, and it stays moved. A change failing either test
    is ignored and the offset carries on. The one unconfirmed reading allowed
    is the last, because it is the offset in force now and waiting a week to
    believe it would be worse than believing it.

    Each step is keyed by the first bar after the weekend the switch happened
    in. Both ends of that weekend are known: the change shows up at one Friday
    close and not at the one before it, and the only Sunday between them is the
    one inside that gap. No trade is stamped while the market is shut, so
    nothing lands inside it.
    """
    steps: list[tuple[datetime.datetime, int]] = []
    for index, weekend in enumerate(weekends):
        following = weekends[index + 1] if index + 1 < len(weekends) else weekend
        if weekend.offset_minutes != following.offset_minutes:
            continue
        if not steps:
            steps.append((from_label, weekend.offset_minutes))
            continue
        current = steps[-1][1]
        if weekend.offset_minutes == current:
            continue
        if abs(weekend.offset_minutes - current) > _MAX_SWITCH_MINUTES:
            continue
        steps.append((weekends[index - 1].opens_at, weekend.offset_minutes))
    return tuple(steps)


def measure_server_clock(terminal: Any, symbols: Iterable[str] = ()) -> ServerClock:
    """The server's offset from real UTC, weekend by weekend.

    MT5 has no call for "what timezone is the server on". Every timestamp it
    hands out is stamped in that clock while looking exactly like a UTC epoch,
    and terminal_info and account_info carry no time at all. The one clock the
    API exposes outside history is the last quote's, which is the wrong thing
    to measure against: quotes stop at the weekend, and a tick left over from
    Friday reads as a plausible offset for the next fourteen hours rather than
    an obvious error.

    The trading week is the anchor instead. It ends at 17:00 in New York, a
    known instant in real UTC for any date, and shows up in the bars as the
    edge of the weekend gap. The difference is the offset - and reading every
    weekend rather than one gives the offset as it stood on each of them, so a
    broker on summer time in July and winter time in January is converted
    correctly in both. One number cannot do that.

    Bars are pulled by position, so no datetime is sent to the terminal and
    nothing can be reinterpreted on the way in. Over ten years of hourly
    history, 496 of 502 weekends agreed with the zone the broker turned out to
    be on; the six that did not were Christmas and New Year sessions that
    closed early, and the step rules drop all six.
    """
    candidates: list[str] = []
    for symbol in (*symbols, *_PROBE_SYMBOLS):
        if symbol and symbol not in candidates:
            candidates.append(symbol)

    for symbol in candidates:
        try:
            terminal.mt5.symbol_select(symbol, True)
            bars = terminal.mt5.copy_rates_from_pos(
                symbol, terminal.mt5.TIMEFRAME_H1, 0, _SCAN_BARS
            )
        except Exception:
            continue
        if bars is None or len(bars) < 2:
            continue

        weekends = _weekends(bars)
        if len(weekends) < _MIN_WEEKENDS:
            continue

        clock = ServerClock(steps=_steps(_label(bars[0]["time"]), weekends))
        if not clock.known:
            continue
        log_event(
            logger,
            "info",
            "terminal.clock.measured",
            symbol=symbol,
            offset_minutes=clock.offset_minutes,
            weekends=len(weekends),
            switches=len(clock.steps) - 1,
            measured_from=clock.steps[0][0].isoformat(),
        )
        return clock

    log_event(logger, "warning", "terminal.clock.unknown", tried=len(candidates))
    return ServerClock()
