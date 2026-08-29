from __future__ import annotations


def round8(value: float) -> float:
    return round(float(value), 8)


def abs8(value: float) -> float:
    return round8(abs(float(value)))


def weighted_price(items: list[tuple[float, float]]) -> float:
    total_volume = 0.0
    weighted = 0.0
    for volume, price in items:
        total_volume += volume
        weighted += volume * price
    if total_volume == 0:
        return 0.0
    return weighted / total_volume
