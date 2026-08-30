from __future__ import annotations

from datetime import datetime, timedelta, timezone

# The floor for a full history pull. It is not the unix epoch on purpose:
# the MetaTrader5 extension converts these datetimes through the platform's
# local-time functions, and on Windows those probe a day either side to
# resolve DST. Anything within a day or so of the epoch pushes that probe
# below zero, where Windows answers EINVAL - which surfaces as
# "history_deals_get returned a result with an exception set". No broker has
# deals from the seventies, so starting well clear of it costs nothing.
EPOCH = datetime(1971, 1, 1, tzinfo=timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def cutoff_from_days(days: int | None) -> datetime | None:
    if not days or days <= 0:
        return None
    return utc_now() - timedelta(days=days)


def history_range(days: int | None) -> tuple[datetime, datetime]:
    end = utc_now() + timedelta(days=1)
    start = cutoff_from_days(days) or EPOCH
    return start, end


def as_server_time(value: datetime | None) -> datetime | None:
    """A server-clock timestamp, without a timezone it does not have.

    MT5 stamps every time it hands out in the trade server's clock, and the
    package presents them as if they were UTC epochs. Carrying that marker
    through to the API would claim UTC for something that is not, and a
    consumer parsing it would be wrong by the offset - more confidently than
    before, because the field looks authoritative. The `*Utc` twin carries the
    marker instead, having earned it.
    """
    if value is None:
        return None
    return value.replace(tzinfo=None)


def as_mt5_time(value: datetime) -> datetime:
    """A server-clock timestamp, in the form the terminal reads back exactly.

    Every datetime handed to MT5 is reduced to a unix epoch and compared
    straight against the bar and deal stamps, which are server wall-clock
    times dressed as UTC epochs. So the argument has to be the server wall
    clock labelled UTC, and then the comparison is exact.

    A naive datetime does not survive that trip: the package resolves it
    through the *machine's* timezone, and the epoch that arrives is out by
    whatever the machine happens to be set to. Measured here, on a machine on
    Eastern European time, asking for 12:00 handed back bars stamped 09:00 in
    summer and 10:00 in winter - the machine's own offset both times, and the
    same for every broker, because the broker never came into it. Tagging the
    argument UTC makes that conversion the identity and the shift disappears.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
