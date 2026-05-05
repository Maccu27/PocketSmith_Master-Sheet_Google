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
    id: int
    name: str
    institution: str | None
    current_balance: float
    currency: str
    is_net_worth: bool
    transaction_account_ids: list[int]


@dataclass(frozen=True)
class Transaction:
    id: int
    account_id: int
    transaction_account_id: int
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
        uid = self.user_id()
        data = self._request("GET", f"/users/{uid}/accounts").json()
        accounts: list[Account] = []
        for raw in data:
            tx_account_ids = [int(ta["id"]) for ta in raw.get("transaction_accounts") or []]
            primary_currency = raw.get("currency_code") or "EUR"
            institution_name = None
            for ta in raw.get("transaction_accounts") or []:
                inst = ta.get("institution") or {}
                if inst.get("title"):
                    institution_name = inst["title"]
                    break
            accounts.append(
                Account(
                    id=int(raw["id"]),
                    name=str(raw.get("title") or "<unbenannt>"),
                    institution=institution_name,
                    current_balance=float(raw.get("current_balance") or 0.0),
                    currency=primary_currency.upper(),
                    is_net_worth=bool(raw.get("is_net_worth", True)),
                    transaction_account_ids=tx_account_ids,
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
        params: dict[str, Any] = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "per_page": PER_PAGE,
            "page": 1,
        }
        path = f"/accounts/{account_id}/transactions"
        while True:
            response = self._request("GET", path, params=params)
            page = response.json() or []
            for raw in page:
                yield Transaction(
                    id=int(raw["id"]),
                    account_id=account_id,
                    transaction_account_id=int(raw["transaction_account"]["id"]),
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
