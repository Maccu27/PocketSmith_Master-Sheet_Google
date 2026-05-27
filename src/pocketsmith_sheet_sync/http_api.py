"""HTTP-API für externen Konsum (finance-agent-system, Variante A).

Drei Endpoints, alle synchron via FastAPI:
- GET  /completeness_check?account=...&year=...&month=...
- GET  /transaction_match?account=...&ps_id=...
- POST /trigger_refresh    (Body: {"account": "..."} optional)
- GET  /health

Lokal starten:
    uvicorn pocketsmith_sheet_sync.http_api:app --port 8001

Optional: für Auth ein Bearer-Token via Env-Variable PSSS_HTTP_TOKEN
(wenn gesetzt, müssen Requests den Header Authorization: Bearer <token>
mitliefern).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from .completeness import (
    get_completeness_check,
    get_transaction_match,
    trigger_refresh,
)

log = logging.getLogger(__name__)

app = FastAPI(
    title="pocketsmith-sheet-sync HTTP-API",
    description="Master-Sheet-Status-API für finance-agent-system (Variante A).",
    version="0.1.0",
)

REQUIRED_TOKEN = os.environ.get("PSSS_HTTP_TOKEN")


def _check_auth(authorization: Optional[str]) -> None:
    """Bearer-Token-Auth wenn PSSS_HTTP_TOKEN gesetzt."""
    if not REQUIRED_TOKEN:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authorization-Header fehlt oder ungültig.")
    token = authorization[len("Bearer "):]
    if token != REQUIRED_TOKEN:
        raise HTTPException(401, "Token ungültig.")


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict:
    """Liveness-Check."""
    return {"status": "ok", "service": "pocketsmith-sheet-sync"}


@app.get("/completeness_check")
def completeness_check(
    account: str = Query(..., description="Konto-Name oder Substring (z.B. 'DKB Girokonto')"),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """Anzahl/Saldo Bank vs PocketSmith für ein Konto + Monat."""
    _check_auth(authorization)
    result = get_completeness_check(account, year, month)
    return result.to_dict()


@app.get("/transaction_match")
def transaction_match(
    account: str = Query(..., description="Konto-Name oder Substring"),
    ps_id: int = Query(..., description="PocketSmith-Transaction-ID"),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """Prüft ob die PS-Buchung im Kontoauszug-Parser eine korrespondierende Bank-Tx hat."""
    _check_auth(authorization)
    return get_transaction_match(account, ps_id)


class TriggerRefreshBody(BaseModel):
    account: Optional[str] = None


@app.post("/trigger_refresh")
def trigger_refresh_endpoint(
    body: TriggerRefreshBody = TriggerRefreshBody(),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """Manuell einen Master-Sheet-Refresh anstoßen.

    ACHTUNG: kann mehrere Minuten dauern (sync_year synchronisiert das ganze Jahr).
    """
    _check_auth(authorization)
    return trigger_refresh(body.account)


def main() -> None:
    """CLI-Entry-Point: `pocketsmith-sync-http`."""
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    port = int(os.environ.get("PSSS_HTTP_PORT", "8001"))
    host = os.environ.get("PSSS_HTTP_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
