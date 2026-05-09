"""PayPal-spezifische Cashflow-Klassifikation.

Aus einem Jahres- oder Monatsauszug von PayPal extrahiert Anthropic die
vollständige Tx-Liste mit `tx_type` und `status`. Aus der Sicht von Marco's
PocketSmith-PayPal-Konto sind aber nur **echte Cashflows** auf dem PayPal-
Saldo relevant. Bezahlungen, bei denen das Geld direkt vom verknüpften
Bankkonto eingezogen wurde (Pass-Through), werden in PocketSmith im
**Bankkonto** getrackt, nicht im PayPal-Konto.

Dieser Klassifikator filtert die rohe Tx-Liste so:
  - Reversed/Denied → ignored
  - Hilfsbuchungen (Authorization, Order, Currency Conversion) → ignored
  - Outflow gepaart mit Funding-Source am gleichen Tag → beide passthrough
  - Refund gepaart mit Card-Withdrawal am gleichen Tag → beide passthrough
  - alles andere → cashflow
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

# Tx-Types aus PayPal-Auszügen
FUNDING_TYPES = {
    "General Card Deposit",
    "PayPal Buyer Credit Payment Funding",
    "Bank Deposit to PP Account",
    "Funds Payable",
    "Non Reference Credit Payment",
}
OUTFLOW_TYPES = {
    "Express Checkout Payment",
    "PreApproved Payment Bill User Payment",
    "General Payment",
    "Donation Payment",
    "Mass Pay Payment",
    "Mobile Payment",
}
REFUND_INFLOW_TYPES = {
    "Payment Refund",
    "Payment Reversal",
}
REFUND_OUTFLOW_TYPES = {
    "General Card Withdrawal",
    "General Buyer Credit Payment",
    "Funds Receivable",
    "Reversal of ACH Deposit",
}
# Hilfsbuchungen die immer ignoriert werden (Reservierungen, Wrapper)
IGNORE_TYPES = {
    "General Authorization",
    "Void of Authorization",
    "Order",
    "Other",  # z. B. StockX 1$ Verify
    "General Currency Conversion",
}
# Diese sind ECHTER PayPal-Cashflow wenn ungepart
CASHFLOW_DEFAULT_TYPES = {
    "User Initiated Withdrawal",
    "Mobile Payment",
}


@dataclass(frozen=True)
class ClassifiedTx:
    index: int
    category: str  # "cashflow", "passthrough", "ignored"


def classify_paypal_transactions(transactions: list) -> dict[int, str]:
    """
    Klassifiziert jede Tx als 'cashflow', 'passthrough' oder 'ignored'.

    Rückgabe: dict mit Index -> Kategorie.

    Eingabe: Liste von Objekten mit den Attributen
      - date (str, YYYY-MM-DD)
      - amount (float)
      - tx_type (str)
      - status (str)
    """
    result: dict[int, str] = {}

    # 1. Status-Filter
    for i, tx in enumerate(transactions):
        if tx.status and tx.status.lower() in ("reversed", "denied"):
            result[i] = "ignored"
            continue
        if tx.tx_type in IGNORE_TYPES:
            result[i] = "ignored"

    # 2. Pairing pro Tag
    by_day: dict[str, list[int]] = defaultdict(list)
    for i, tx in enumerate(transactions):
        if i in result:
            continue
        by_day[tx.date].append(i)

    for day, idxs in by_day.items():
        # Funding-Pairs: Outflow (negativ) + Funding (positiv, gleicher |Betrag|)
        used = set()
        for i in idxs:
            if i in used or i in result:
                continue
            tx_i = transactions[i]
            if tx_i.amount >= 0 or tx_i.tx_type not in OUTFLOW_TYPES:
                continue
            for j in idxs:
                if j == i or j in used or j in result:
                    continue
                tx_j = transactions[j]
                if tx_j.amount <= 0:
                    continue
                if tx_j.tx_type not in FUNDING_TYPES:
                    continue
                if abs(tx_j.amount + tx_i.amount) < 0.01:  # tx_i ist negativ
                    result[i] = "passthrough"
                    result[j] = "passthrough"
                    used.add(i)
                    used.add(j)
                    break

        # Refund-Pairs: Refund (positiv) + Withdrawal (negativ, gleicher Betrag)
        for i in idxs:
            if i in used or i in result:
                continue
            tx_i = transactions[i]
            if tx_i.amount <= 0 or tx_i.tx_type not in REFUND_INFLOW_TYPES:
                continue
            for j in idxs:
                if j == i or j in used or j in result:
                    continue
                tx_j = transactions[j]
                if tx_j.amount >= 0:
                    continue
                if tx_j.tx_type not in REFUND_OUTFLOW_TYPES:
                    continue
                if abs(tx_j.amount + tx_i.amount) < 0.01:
                    result[i] = "passthrough"
                    result[j] = "passthrough"
                    used.add(i)
                    used.add(j)
                    break

    # 3. Default: alles andere ist echter Cashflow
    for i in range(len(transactions)):
        if i not in result:
            result[i] = "cashflow"

    return result


def filter_to_cashflow(transactions: list) -> list:
    """Convenience-Wrapper: gibt nur die Tx zurück die als cashflow klassifiziert wurden."""
    classifications = classify_paypal_transactions(transactions)
    return [tx for i, tx in enumerate(transactions) if classifications.get(i) == "cashflow"]


def is_paypal_format(transactions: list) -> bool:
    """Heuristik: Anthropic hat tx_type/status gefüllt → es ist ein PayPal-Format-Auszug."""
    if not transactions:
        return False
    return any(getattr(tx, "tx_type", "") for tx in transactions[:10])
