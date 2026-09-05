from __future__ import annotations


def round8(value: float) -> float:
    return round(float(value), 8)


def abs8(value: float) -> float:
    return round8(abs(float(value)))


def weighted_price(items: list[tuple[float, float]]) -> float:
    total_volume = sum(volume for volume, _ in items)
    if total_volume == 0:
        return 0.0
    return sum(volume * price for volume, price in items) / total_volume
