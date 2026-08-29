from __future__ import annotations

from datetime import datetime, timedelta, timezone

EPOCH = datetime(1970, 1, 2, tzinfo=timezone.utc)


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
