from __future__ import annotations

from dataclasses import dataclass

MINUTE = "1m"
DAY = "1d"

_DAY_MS = 24 * 60 * 60 * 1000
_MIN_SPAN_FOR_DAILY = 2 * _DAY_MS


@dataclass(frozen=True, slots=True)
class Segment:
    interval: str
    start_ms: int
    end_ms: int


def split(start_ms: int, end_ms: int) -> list[Segment]:
    minutes = [Segment(MINUTE, start_ms, end_ms)]

    if end_ms <= start_ms or end_ms - start_ms < _MIN_SPAN_FOR_DAILY:
        return minutes

    first_midnight = start_ms - start_ms % _DAY_MS
    if start_ms % _DAY_MS != 0:
        first_midnight += _DAY_MS
    last_midnight = end_ms - end_ms % _DAY_MS
    if last_midnight <= first_midnight:
        return minutes

    segments: list[Segment] = []
    if start_ms < first_midnight:
        segments.append(Segment(MINUTE, start_ms, first_midnight - 1))
    segments.append(Segment(DAY, first_midnight, last_midnight - 1))
    if last_midnight <= end_ms:
        segments.append(Segment(MINUTE, last_midnight, end_ms))
    return segments
