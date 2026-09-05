from __future__ import annotations

from domain import fx

from .. import raw
from ..numbers import abs8


def build_transactions(deals: list[raw.RawDeal]) -> list[fx.Transaction]:
    out: list[fx.Transaction] = []
    for deal in deals:
        if deal.type not in raw.BALANCE_DEAL_TYPES:
            continue
        amount = abs8(deal.profit)
        if amount == 0:
            continue
        out.append(fx.Transaction(
            time=deal.time,
            type=fx.TRANSACTION_TYPE_DEPOSIT if deal.profit > 0 else fx.TRANSACTION_TYPE_WITHDRAWAL,
            amount=amount,
        ))
    out.sort(key=lambda item: (item.time is None, item.time))
    return out
