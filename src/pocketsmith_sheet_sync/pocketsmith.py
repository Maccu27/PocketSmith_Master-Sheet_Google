from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterator

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.pocketsmith.com/v2"
PER_PAGE = 100


@dataclass(frozen=True)
class Account:
    """Eine Zeile in der Konten-Tab = ein PocketSmith **transaction_account**.

    Wir verwenden die fein-granulare Ebene (93 statt 53), damit archivierte
    Sammel-Accounts (z. B. "Archivierte Konten (Investments-EUR)") in ihre
    einzelnen Sub-Konten aufgelöst werden — pro IBAN/Konto eine Zeile.
    """

    id: int                            # transaction_account ID
    parent_account_id: int             # ID des übergeordneten logischen Account
    name: str                          # transaction_account-Name (mit IBAN, falls vorhanden)
    institution: str | None
    current_balance: float
    currency: str
    is_net_worth: bool
    starting_balance: float | None
    starting_balance_date: date | None


@dataclass(frozen=True)
class Transaction:
    id: int
    account_id: int                     # parent account ID
    transaction_account_id: int         # = Account.id in unserem neuen Modell
    date: date
    amount: float
    labels: list[str]
    payee: str | None
    original_payee: str | None
    note: str | None


class PocketSmithClient:
    def __init__(self, api_key: str, *, timeout: float = 30.0):
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"X-Developer-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
        )
        self._user_id: int | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PocketSmithClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(4):
            response = self._client.request(method, path, **kwargs)
            if response.status_code == 429:
                wait = float(response.headers.get("Retry-After", 2 ** attempt))
                log.warning("rate-limited, sleeping %.1fs", wait)
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response
        raise RuntimeError(f"too many retries for {method} {path}")

    def user_id(self) -> int:
        if self._user_id is None:
            data = self._request("GET", "/me").json()
            self._user_id = int(data["id"])
        return self._user_id

    def list_accounts(self) -> list[Account]:
        """Liefert ALLE transaction_accounts (93 Stück), nicht die logischen accounts.

        Damit erscheint jedes archivierte Sub-Konto einzeln in der Sheet — z. B.
        die 26 Konten unter "Archivierte Konten (Investments-EUR)".
        """
        uid = self.user_id()
        data = self._request("GET", f"/users/{uid}/transaction_accounts").json()
        accounts: list[Account] = []
        for raw in data:
            inst = raw.get("institution") or {}
            currency = (raw.get("currency_code") or "EUR").upper()
            sb_raw = raw.get("starting_balance")
            sb_date_raw = raw.get("starting_balance_date")
            accounts.append(
                Account(
                    id=int(raw["id"]),
                    parent_account_id=int(raw.get("account_id") or 0),
                    name=str(raw.get("name") or "<unbenannt>"),
                    institution=inst.get("title"),
                    current_balance=float(raw.get("current_balance") or 0.0),
                    currency=currency,
                    is_net_worth=bool(raw.get("is_net_worth", True)),
                    starting_balance=float(sb_raw) if sb_raw is not None else None,
                    starting_balance_date=date.fromisoformat(sb_date_raw) if sb_date_raw else None,
                )
            )
        return accounts

    def iter_transactions(
        self,
        account_id: int,
        *,
        start_date: date,
        end_date: date,
    ) -> Iterator[Transaction]:
        """Lädt Transaktionen für ein transaction_account (= Account.id in unserem Modell)."""
        params: dict[str, Any] = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "per_page": PER_PAGE,
            "page": 1,
        }
        path = f"/transaction_accounts/{account_id}/transactions"
        while True:
            response = self._request("GET", path, params=params)
            page = response.json() or []
            for raw in page:
                ta_obj = raw.get("transaction_account") or {}
                yield Transaction(
                    id=int(raw["id"]),
                    account_id=int(ta_obj.get("account_id") or 0),
                    transaction_account_id=int(ta_obj.get("id") or account_id),
                    date=date.fromisoformat(raw["date"]),
                    amount=float(raw.get("amount") or 0.0),
                    labels=list(raw.get("labels") or []),
                    payee=raw.get("payee"),
                    original_payee=raw.get("original_payee"),
                    note=raw.get("note"),
                )
            link = response.headers.get("Link", "")
            if 'rel="next"' not in link:
                return
            params["page"] = int(params["page"]) + 1
