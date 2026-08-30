from __future__ import annotations

from ..raw import RawDeal
from .mathutil import round8


def sort_deals(deals: list[RawDeal]) -> list[RawDeal]:
    return sorted(deals, key=lambda deal: (deal.time_msc, deal.ticket))


def balance_after_close(deals: list[RawDeal], current_balance: float) -> dict[int, float]:
    """The account balance the moment each position finished closing.

    cTrader puts the balance on every closing deal; MT5 deals carry none. So
    the ledger is rebuilt instead: every deal's effect on the balance summed and
    subtracted from the *current* balance recovers where the account started,
    then replaying forward records the balance after each position's last close.

    This is why a sync always pulls the whole history - a windowed pull would
    recover the wrong starting balance and shift every figure after it.
    """
    running = round8(current_balance - sum(deal.balance_delta for deal in deals))

    balances: dict[int, float] = {}
    for deal in sort_deals(deals):
        running = round8(running + deal.balance_delta)
        if deal.is_trading and deal.position_id and deal.is_closing:
            balances[deal.position_id] = running
    return balances
