"""Vollständigkeits-Check-API für externen Konsum (finance-agent-system, Variante A).

Drei reine Python-Funktionen, die von:
- mcp_server.py als MCP-Tools exportiert werden
- http_api.py als REST-Endpoints exportiert werden

Logik:
- PocketSmith-Seite: aggregate_year() liefert MonthlyStats pro Monat (count + eom_balance)
- Bank-Auszug-Seite: aggregate_pdf_data_for_month() liefert (soll_count, soll_balance)
- Vergleich: Diff von Anzahl + Saldo; Status ok wenn beides matched, sonst warn
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Optional

from .aggregator import aggregate_year
from .config import Settings, load_settings
from .pdf_sync import aggregate_pdf_data_for_month, find_account_by_id
from .pdf_tracker import PDFTracker
from .pocketsmith import PocketSmithClient
from .sheets import SheetsClient

log = logging.getLogger(__name__)

SALDO_TOLERANCE_EUR = 0.01


@dataclass
class CompletenessResult:
    """Strukturiertes Ergebnis für eine Konto+Monat-Kombination."""
    account_name: str
    account_id: Optional[int]
    year: int
    month: int
    anzahl_bank: Optional[int]
    anzahl_ps: Optional[int]
    saldo_bank: Optional[float]
    saldo_ps: Optional[float]
    status: str  # ok | warn | not_available
    letzter_cron: Optional[str]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _find_account_by_name(accounts: list, partial: str):
    """Finde Account dessen Name die Substring enthält (case-insensitive)."""
    target = partial.lower()
    for acc in accounts:
        if target in acc.name.lower():
            return acc
    return None


def _last_cron_marker() -> str:
    """ISO-Datetime des aktuellen Aufrufs (Cron-Lauf-Status approximation).

    Im produktiven Setup würde man hier den Timestamp des letzten erfolgreichen
    Daily-Jobs aus einer Status-Datei lesen. Für jetzt: aktueller Aufruf.
    """
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────


def get_completeness_check(
    account_name: str,
    year: int,
    month: int,
    *,
    settings: Optional[Settings] = None,
) -> CompletenessResult:
    """Vollständigkeits-Check für ein Konto + Monat.

    Args:
        account_name: Substring des PocketSmith-Account-Namens (z.B. "DKB Girokonto")
        year: Jahr (z.B. 2026)
        month: 1-12

    Returns:
        CompletenessResult mit anzahl/saldo Bank vs. PS und Status.
    """
    if settings is None:
        settings = load_settings()

    notes: list[str] = []

    # === PocketSmith-Seite ===
    try:
        with PocketSmithClient(settings.pocketsmith_api_key) as ps:
            accounts = ps.list_accounts()
            account = _find_account_by_name(accounts, account_name)
            if not account:
                return CompletenessResult(
                    account_name=account_name, account_id=None,
                    year=year, month=month,
                    anzahl_bank=None, anzahl_ps=None,
                    saldo_bank=None, saldo_ps=None,
                    status="not_available",
                    letzter_cron=_last_cron_marker(),
                    notes=[f"Konto '{account_name}' in PocketSmith nicht gefunden."],
                )

            today = date.today()
            txs = list(ps.iter_transactions(
                account.id,
                start_date=date(year, 1, 1),
                end_date=today if today.year == year else date(year, 12, 31),
            ))
            stats = aggregate_year(
                account, txs,
                year=year, today=today,
                verified_label=settings.verified_label,
            )
            month_stats = stats.months.get(month)
            anzahl_ps = month_stats.count_effective if month_stats else None
            saldo_ps = month_stats.end_of_month_balance if month_stats else None
    except Exception as exc:
        log.exception("PocketSmith-Fehler beim Completeness-Check")
        return CompletenessResult(
            account_name=account_name, account_id=None,
            year=year, month=month,
            anzahl_bank=None, anzahl_ps=None,
            saldo_bank=None, saldo_ps=None,
            status="not_available",
            letzter_cron=_last_cron_marker(),
            notes=[f"PocketSmith-Fehler: {exc.__class__.__name__}: {exc}"],
        )

    # === Bank-Auszug-Seite (PDF-Tracker) ===
    anzahl_bank: Optional[int] = None
    saldo_bank: Optional[float] = None

    try:
        cred_info = settings.google_credentials_info()
        sheets = SheetsClient(cred_info)
        # Lazy: nur wenn DRIVE_FINANZEN_FOLDER_ID gesetzt
        if settings.drive_finanzen_folder_id:
            from .drive_client import DriveClient
            drive = DriveClient(cred_info)
            tracker = PDFTracker(
                sheets, drive,
                finanzen_folder_id=settings.drive_finanzen_folder_id,
                explicit_sheet_id=settings.pdf_tracking_sheet_id,
            )
            records = tracker.all_parsed_records()
            anzahl_bank, saldo_bank = aggregate_pdf_data_for_month(
                records, account_id=account.id, year=year, month=month,
            )
        else:
            notes.append("PDF-Tracker nicht konfiguriert (DRIVE_FINANZEN_FOLDER_ID fehlt).")
    except Exception as exc:
        log.exception("PDF-Tracker-Fehler beim Completeness-Check")
        notes.append(f"PDF-Tracker-Fehler: {exc.__class__.__name__}")

    # === Status bestimmen ===
    if anzahl_bank is None and saldo_bank is None:
        status = "warn"
        notes.append("Keine Bank-Daten verfügbar — PDF-Auszug wahrscheinlich noch nicht geparst.")
    elif (anzahl_bank == anzahl_ps) and (
        saldo_bank is None or saldo_ps is None or abs(saldo_bank - saldo_ps) < SALDO_TOLERANCE_EUR
    ):
        status = "ok"
    else:
        status = "warn"
        if anzahl_bank != anzahl_ps:
            notes.append(f"Anzahl-Differenz: Bank {anzahl_bank} vs PS {anzahl_ps}")
        if saldo_bank is not None and saldo_ps is not None and abs(saldo_bank - saldo_ps) >= SALDO_TOLERANCE_EUR:
            notes.append(f"Saldo-Differenz: Bank {saldo_bank:.2f} € vs PS {saldo_ps:.2f} €")

    return CompletenessResult(
        account_name=account_name, account_id=account.id,
        year=year, month=month,
        anzahl_bank=anzahl_bank, anzahl_ps=anzahl_ps,
        saldo_bank=saldo_bank, saldo_ps=saldo_ps,
        status=status,
        letzter_cron=_last_cron_marker(),
        notes=notes,
    )


def get_transaction_match(
    account_name: str,
    ps_transaction_id: int,
    *,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """Prüfe ob eine PocketSmith-Buchung im PDF-Auszug-Parser zugeordnet wurde.

    Konservative Implementation: liefert matched=True wenn für das Konto+Monat
    der Tx mindestens ein ParsedRecord existiert (d.h. der Auszug wurde geparst).
    Volle Bank-Tx-zu-PS-Tx-Zuordnung kommt später wenn die PocketSmith-IDs
    direkt in den ParsedRecord.transactions persistiert werden.

    Args:
        account_name: Substring des Account-Namens
        ps_transaction_id: PocketSmith-Tx-ID

    Returns:
        {matched: bool, auszug_betrag: float|None, auszug_memo: str|None, auszug_datum: str|None, notes: [str]}
    """
    if settings is None:
        settings = load_settings()

    result: dict[str, Any] = {
        "matched": False,
        "auszug_betrag": None,
        "auszug_memo": None,
        "auszug_datum": None,
        "notes": [],
    }

    try:
        with PocketSmithClient(settings.pocketsmith_api_key) as ps:
            accounts = ps.list_accounts()
            account = _find_account_by_name(accounts, account_name)
            if not account:
                result["notes"].append(f"Konto '{account_name}' nicht gefunden.")
                return result

            # Tx aus PocketSmith holen
            today = date.today()
            txs = list(ps.iter_transactions(
                account.id,
                start_date=date(today.year, 1, 1),
                end_date=today,
            ))
            target_tx = next((t for t in txs if t.id == ps_transaction_id), None)
            if not target_tx:
                result["notes"].append(f"PS-Tx {ps_transaction_id} im Konto {account_name} nicht gefunden.")
                return result

        # Tracker prüfen
        if not settings.drive_finanzen_folder_id:
            result["notes"].append("PDF-Tracker nicht konfiguriert.")
            return result

        cred_info = settings.google_credentials_info()
        sheets = SheetsClient(cred_info)
        from .drive_client import DriveClient
        drive = DriveClient(cred_info)
        tracker = PDFTracker(
            sheets, drive,
            finanzen_folder_id=settings.drive_finanzen_folder_id,
            explicit_sheet_id=settings.pdf_tracking_sheet_id,
        )
        records = tracker.all_parsed_records()
        # Records für dieses Konto + diesen Monat
        rec_match = [
            r for r in records
            if r.matched_account_id == account.id
            and any(
                tx.get("date", "").startswith(f"{target_tx.date.year:04d}-{target_tx.date.month:02d}")
                for tx in r.transactions
            )
        ]
        if rec_match:
            result["matched"] = True
            result["auszug_datum"] = target_tx.date.isoformat()
            result["notes"].append(
                f"{len(rec_match)} Auszug-Record(s) für {account_name} im "
                f"{target_tx.date.year}-{target_tx.date.month:02d} gefunden. "
                f"Exakte Tx-zu-Tx-Zuordnung noch nicht implementiert."
            )
        else:
            result["notes"].append(
                f"Kein Auszug für {account_name} im "
                f"{target_tx.date.year}-{target_tx.date.month:02d} im Tracker."
            )
    except Exception as exc:
        log.exception("Fehler bei get_transaction_match")
        result["notes"].append(f"Fehler: {exc.__class__.__name__}: {exc}")

    return result


def list_records_for_month(
    account_name: str,
    year: int,
    month: int,
    *,
    settings: Optional[Settings] = None,
) -> list[dict[str, Any]]:
    """Liste aller ParsedRecord-Daten für ein Konto+Monat.

    Wird vom finance-agent-Dashboard aufgerufen, um beim Aufklappen des
    Vollständigkeits-Check-Blocks die Detail-Liste der verarbeiteten PDFs
    zu zeigen — inklusive Drive-Datei-ID für direkten Link.

    Returns:
        Liste von Dicts mit file_id, path, parsed_at, bank_name,
        statement_period_start/end, transaction_count, starting/ending_balance,
        notes, confidence. Leere Liste wenn nichts gefunden.
    """
    if settings is None:
        settings = load_settings()

    try:
        cred_info = settings.google_credentials_info()
        sheets = SheetsClient(cred_info)
        if not settings.drive_finanzen_folder_id:
            return []
        from .drive_client import DriveClient
        drive = DriveClient(cred_info)
        tracker = PDFTracker(
            sheets, drive,
            finanzen_folder_id=settings.drive_finanzen_folder_id,
            explicit_sheet_id=settings.pdf_tracking_sheet_id,
        )
        records = tracker.all_parsed_records()
    except Exception as exc:
        log.exception("Fehler beim Laden von Tracker-Records")
        return []

    # Account-ID resolvieren
    try:
        with PocketSmithClient(settings.pocketsmith_api_key) as ps:
            accounts = ps.list_accounts()
            account = _find_account_by_name(accounts, account_name)
            if not account:
                return []
    except Exception:
        return []

    # Filter: nur Records für dieses Konto + Monat (Stichtag)
    matched = [
        r for r in records
        if r.matched_account_id == account.id
        and r.year == year
        and r.month == month
    ]

    return [
        {
            "file_id": r.file_id,
            "path": r.path,
            "parsed_at": r.parsed_at,
            "bank_name": r.bank_name,
            "statement_period_start": r.statement_period_start,
            "statement_period_end": r.statement_period_end,
            "starting_balance": r.starting_balance,
            "ending_balance": r.ending_balance,
            "transaction_count": r.transaction_count,
            "confidence": r.confidence,
            "notes": r.notes,
            "drive_url": f"https://drive.google.com/file/d/{r.file_id}/view",
        }
        for r in matched
    ]


def trigger_refresh(account_name: Optional[str] = None) -> dict[str, Any]:
    """Triggert manuell einen Master-Sheet-Refresh.

    Args:
        account_name: optional. Aktuell ignoriert — sync_year synchronisiert
                      immer das ganze Jahr für alle konfigurierten Konten.

    Returns:
        {status: 'started' | 'error', message: str}
    """
    from .sync import sync_year as _sync_year_impl

    try:
        settings = load_settings()
        today = date.today()
        year = today.year
        sheet_id = settings.sheets_per_year.get(year)
        if not sheet_id:
            return {
                "status": "error",
                "message": f"Kein Sheet für {year} konfiguriert. SYNC_YEARS und MASTER_SHEET_{year} prüfen.",
            }

        cred_info = settings.google_credentials_info()
        gs = SheetsClient(cred_info)

        with PocketSmithClient(settings.pocketsmith_api_key) as ps:
            _sync_year_impl(
                ps, gs,
                spreadsheet_id=sheet_id,
                year=year, today=today,
                verified_label=settings.verified_label,
            )

        return {
            "status": "started",
            "message": f"Sync für {year} erfolgreich ausgeführt. Sheet-ID: {sheet_id}",
            "year": year,
            "ignored_account_hint": account_name,
        }
    except Exception as exc:
        log.exception("trigger_refresh fehlgeschlagen")
        return {"status": "error", "message": f"{exc.__class__.__name__}: {exc}"}
