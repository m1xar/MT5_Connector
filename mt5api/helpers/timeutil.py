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


def to_real_utc(value: datetime | None, offset_minutes: int | None) -> datetime | None:
    """The same instant with the trade server's offset taken off.

    Everything MT5 hands out is stamped in the server's clock while looking
    exactly like a UTC epoch, so that is what got stored. Undoing it is a
    read-side concern, like the `days` window: the stored value stays the one
    the terminal itself would show.

    None when the offset is unknown - a wrong answer here is worse than no
    answer, and the offset can only be measured while the market quotes.
    """
    if value is None or offset_minutes is None:
        return None
    return value - timedelta(minutes=offset_minutes)


def as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def is_after_cutoff(value: datetime | None, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    if value is None:
        return False
    return not value < cutoff
