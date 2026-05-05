from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .pocketsmith import Account, Transaction


@dataclass(frozen=True)
class MonthlyStats:
    year: int
    month: int
    count_technical: int        # rohe Anzahl PocketSmith-Tx
    count_effective: int         # Anzahl nach Split-Gruppierung
    count_verified_effective: int  # Anzahl effektive Tx, in denen ALLE Splits verifiziert sind
    end_of_month_balance: float | None  # None = echte Zukunft


@dataclass(frozen=True)
class AccountYearStats:
    account: Account
    months: dict[int, MonthlyStats]
    total_count_technical: int
    total_count_effective: int
    total_verified_effective: int


def end_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _group_key(t: Transaction) -> tuple:
    """Schlüssel zur Erkennung wahrscheinlicher Splits.

    Eine Bank-Transaktion, die in PocketSmith in mehrere Teile gesplittet wurde,
    behält normalerweise gleiches date + original_payee + note + transaction_account.
    Risiko: 2 echt separate Bank-Tx mit identischen Feldern werden auch
    zusammengeworfen — bewusst akzeptiert.
    """
    return (
        t.date,
        t.payee or "",  # placeholder, see below
    )


def group_transactions_by_split(transactions: list[Transaction]) -> list[list[Transaction]]:
    """Gruppiere Tx nach (date, original_payee, note, transaction_account_id)."""
    groups: dict[tuple, list[Transaction]] = defaultdict(list)
    for t in transactions:
        key = (
            t.date,
            (t.payee or "").strip(),  # we use 'payee' as fallback; adjusted below
        )
        groups[key].append(t)
    return list(groups.values())


def aggregate_year(
    account: Account,
    transactions: Iterable[Transaction],
    *,
    year: int,
    today: date,
    verified_label: str,
) -> AccountYearStats:
    """Aggregate monthly counts and end-of-month balances for one account/year.

    Balance logic: eom_balance(M) = current_balance - sum(tx.amount where tx.date > eom(M)).
    Requires that `transactions` covers [Y-01-01, today].
    Future months: None. Current month with eom > today: shows current_balance (today's stand).
    """
    txs = list(transactions)
    months: dict[int, MonthlyStats] = {}

    for m in range(1, 13):
        eom = end_of_month(year, m)

        in_month = [t for t in txs if t.date.year == year and t.date.month == m]

        # Technical count = rohe Anzahl
        count_technical = len(in_month)

        # Effective count = nach Gruppierung (date+original_payee+note+account)
        groups: dict[tuple, list[Transaction]] = defaultdict(list)
        for t in in_month:
            key = (
                t.date,
                (t.original_payee or "").strip(),
                (t.note or "").strip(),
                t.transaction_account_id,
            )
            groups[key].append(t)
        count_effective = len(groups)

        # Verifiziert = Gruppe gilt als verifiziert wenn ALLE Splits das Label haben
        count_verified_effective = sum(
            1 for grp in groups.values()
            if all(verified_label in t.labels for t in grp)
        )

        # End-of-month balance
        if eom > today:
            if year == today.year and m == today.month:
                balance: float | None = round(account.current_balance, 2)
            else:
                balance = None
        else:
            future_sum = sum(t.amount for t in txs if t.date > eom)
            balance = round(account.current_balance - future_sum, 2)

        months[m] = MonthlyStats(
            year=year,
            month=m,
            count_technical=count_technical,
            count_effective=count_effective,
            count_verified_effective=count_verified_effective,
            end_of_month_balance=balance,
        )

    return AccountYearStats(
        account=account,
        months=months,
        total_count_technical=sum(s.count_technical for s in months.values()),
        total_count_effective=sum(s.count_effective for s in months.values()),
        total_verified_effective=sum(s.count_verified_effective for s in months.values()),
    )
