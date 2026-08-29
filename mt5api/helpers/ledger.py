from __future__ import annotations

from ..raw import RawDeal
from .mathutil import round8


def sort_deals(deals: list[RawDeal]) -> list[RawDeal]:
    return sorted(deals, key=lambda deal: (deal.time_msc, deal.ticket))


def starting_balance(deals: list[RawDeal], current_balance: float) -> float:
    total = sum(deal.balance_delta for deal in deals)
    return round8(current_balance - total)


def balance_after_close(deals: list[RawDeal], current_balance: float) -> dict[int, float]:
    running = starting_balance(deals, current_balance)
    result: dict[int, float] = {}
    for deal in sort_deals(deals):
        running = round8(running + deal.balance_delta)
        if deal.is_trading and deal.position_id and deal.is_closing:
            result[deal.position_id] = running
    return result
