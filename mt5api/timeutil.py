from __future__ import annotations

from datetime import datetime, timedelta, timezone

EPOCH = datetime(1971, 1, 1, tzinfo=timezone.utc)


def cutoff_from_days(days: int | None) -> datetime | None:
    if not days or days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


def history_range(days: int | None = None) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc) + timedelta(days=1)
    return cutoff_from_days(days) or EPOCH, end


def as_server_time(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=None) if value is not None else None


def as_mt5_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
