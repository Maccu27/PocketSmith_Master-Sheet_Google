"""Orchestrator für den PDF-Parse-Workflow.

Crawlt Drive nach Kontoauszug-PDFs, schickt unverarbeitete an Claude, schreibt
Soll-Anzahl + Soll-Saldo in den richtigen Monatstab der Master-Sheet (sofern
das Jahr konfiguriert ist), trackt alles in einem separaten Tracking-Sheet.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from .config import Settings
from .drive_client import DriveClient
from .pdf_extractor import ExtractionResult, PDFExtractor
from .pdf_tracker import PDFTracker, ParsedRecord
from .pocketsmith import Account, PocketSmithClient
from .sheets import SheetsClient
from .sync import KONTEN_TAB, MONAT_HEADERS, monat_tab_name

log = logging.getLogger(__name__)

# Spalten in den Monats-Tabs (vgl. sync.MONAT_HEADERS)
MONAT_COL_SOLL_ANZAHL_LETTER = "C"  # Index 2
MONAT_COL_SOLL_SALDO_LETTER = "F"   # Index 5


def parse_year_month_from_dates(start: str, end: str) -> tuple[int, int]:
    """Bestimme das relevante (year, month) für die Sheet-Zuordnung.

    Wir nehmen den Monat des End-Datums — das ist der "Stichtag" des Auszugs.
    Bei einem Mai-Auszug der DKB läuft der Zeitraum oft 30.04 bis 31.05 → wir
    ordnen das dem Mai zu.
    """
    for raw in (end, start):
        if not raw:
            continue
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d").date()
            return dt.year, dt.month
        except ValueError:
            continue
    raise ValueError(f"Konnte kein Datum aus '{start}' / '{end}' parsen")


def find_account_by_id(accounts: list[Account], account_id: int | None) -> Account | None:
    if account_id is None:
        return None
    for a in accounts:
        if a.id == account_id:
            return a
    return None


def parse_all_new_pdfs(
    settings: Settings,
    *,
    cred_info: dict[str, Any],
    today: date,
) -> dict[str, int]:
    """Hauptfunktion: scannt Drive, verarbeitet neue PDFs, schreibt Master-Sheets."""
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY ist nicht gesetzt")
    if not settings.drive_finanzen_folder_id:
        raise RuntimeError("DRIVE_FINANZEN_FOLDER_ID ist nicht gesetzt")

    drive = DriveClient(cred_info)
    sheets = SheetsClient(cred_info)
    extractor = PDFExtractor(settings.anthropic_api_key, model=settings.anthropic_model)
    tracker = PDFTracker(
        sheets, drive,
        finanzen_folder_id=settings.drive_finanzen_folder_id,
        explicit_sheet_id=settings.pdf_tracking_sheet_id,
    )

    log.info("Scanning Drive folder %s for PDFs under '%s/...'",
             settings.drive_finanzen_folder_id, settings.pdf_kontoauszug_folder_marker)
    pdfs = drive.find_pdfs_under_marker(
        settings.drive_finanzen_folder_id,
        folder_marker=settings.pdf_kontoauszug_folder_marker,
    )
    log.info("Found %d Kontoauszug-PDFs total", len(pdfs))

    processed = tracker.processed_file_ids()
    new_pdfs = [p for p in pdfs if p.id not in processed]
    log.info("Davon %d noch nicht verarbeitet", len(new_pdfs))

    if not new_pdfs:
        log.info("Nichts zu tun")
        return {"total": len(pdfs), "new": 0, "errors": 0, "written_to_sheet": 0}

    # PocketSmith-Accounts für Matching
    log.info("Lade PocketSmith-Konten für Matching")
    with PocketSmithClient(settings.pocketsmith_api_key) as ps:
        accounts = ps.list_accounts()

    sheets_per_year = settings.sheets_per_year
    counters = {"total": len(pdfs), "new": len(new_pdfs), "errors": 0, "written_to_sheet": 0}

    for pdf in new_pdfs:
        log.info("Verarbeite: %s (%.0f KB)", pdf.path, pdf.size / 1024)
        try:
            pdf_bytes = drive.download_bytes(pdf.id)
            result = extractor.extract(pdf_bytes, accounts=accounts, pdf_filename=pdf.name)
        except Exception as exc:
            log.error("Fehler bei %s: %s", pdf.path, exc, exc_info=True)
            tracker.append_error(pdf.id, pdf.path, str(exc))
            counters["errors"] += 1
            continue

        try:
            year, month = parse_year_month_from_dates(
                result.statement_period_start, result.statement_period_end,
            )
        except ValueError as exc:
            log.error("Datum-Parsing fehlgeschlagen für %s: %s", pdf.path, exc)
            tracker.append_error(pdf.id, pdf.path, f"date parse: {exc}")
            counters["errors"] += 1
            continue

        matched = find_account_by_id(accounts, result.matched_account_id)
        record = ParsedRecord(
            file_id=pdf.id,
            path=pdf.path,
            parsed_at=datetime.utcnow().isoformat(timespec="seconds"),
            bank_name=result.bank_name,
            iban_or_account_number=result.iban_or_account_number,
            matched_account_id=result.matched_account_id,
            matched_account_name=matched.name if matched else "",
            currency=result.currency,
            year=year,
            month=month,
            statement_period_start=result.statement_period_start,
            statement_period_end=result.statement_period_end,
            transaction_count=result.transaction_count,
            ending_balance=result.ending_balance,
            confidence=result.confidence,
            notes=result.notes,
        )
        tracker.append_parsed(record)

        # Sofort in Master-Sheet schreiben, wenn das Jahr konfiguriert ist
        master_sheet_id = sheets_per_year.get(year)
        if not master_sheet_id:
            log.info("  → keine Master-Sheet für %d, nur im Tracker gespeichert", year)
            continue
        if not matched:
            log.warning("  → kein Account-Match (confidence=%.2f, notes=%s)",
                        result.confidence, result.notes)
            continue

        try:
            written = write_soll_to_master_sheet(
                sheets, master_sheet_id, year=year, month=month,
                account_name=matched.name,
                soll_count=record.transaction_count,
                soll_balance=record.ending_balance,
            )
            if written:
                counters["written_to_sheet"] += 1
                log.info("  → Soll-Werte in %s/%s eingetragen (Konto: %s)",
                         year, monat_tab_name(year, month), matched.name)
            else:
                log.warning("  → Konto %s nicht in Konten-Tab der Master-Sheet %d gefunden",
                            matched.name, year)
        except Exception as exc:
            log.error("  → Sheet-Update fehlgeschlagen: %s", exc, exc_info=True)
            counters["errors"] += 1

    return counters


def backfill_master_sheet_from_tracker(
    settings: Settings,
    *,
    cred_info: dict[str, Any],
    year: int,
) -> int:
    """Schreibt für ein Jahr alle bereits getrackten Soll-Werte in die Master-Sheet."""
    sheets_per_year = settings.sheets_per_year
    master_sheet_id = sheets_per_year.get(year)
    if not master_sheet_id:
        raise RuntimeError(f"Kein MASTER_SHEET_{year} konfiguriert")

    drive = DriveClient(cred_info)
    sheets = SheetsClient(cred_info)
    tracker = PDFTracker(
        sheets, drive,
        finanzen_folder_id=settings.drive_finanzen_folder_id or "",
        explicit_sheet_id=settings.pdf_tracking_sheet_id,
    )

    records = tracker.all_parsed_records()
    relevant = [r for r in records if r.year == year and r.matched_account_name]
    log.info("Backfill für %d: %d relevante Tracker-Einträge", year, len(relevant))

    written = 0
    for r in relevant:
        try:
            ok = write_soll_to_master_sheet(
                sheets, master_sheet_id, year=year, month=r.month,
                account_name=r.matched_account_name,
                soll_count=r.transaction_count,
                soll_balance=r.ending_balance,
            )
            if ok:
                written += 1
        except Exception as exc:
            log.warning("Backfill skip für %s/%s: %s", r.path, r.matched_account_name, exc)
    return written


def write_soll_to_master_sheet(
    sheets: SheetsClient,
    spreadsheet_id: str,
    *,
    year: int,
    month: int,
    account_name: str,
    soll_count: int,
    soll_balance: float,
) -> bool:
    """Findet die Zeile mit `account_name` im Monatstab und schreibt Soll-Werte.

    Returns True wenn geschrieben wurde, False wenn der Account in der Konten-Tab
    nicht aktiv ist (d. h. die Zeile fehlt in dem Monatstab).
    """
    tab = monat_tab_name(year, month)
    range_a1 = f"{tab}!A1:A300"  # Spalte A = Kontoname
    try:
        result = sheets._sheets.spreadsheets().values().get(  # noqa: SLF001
            spreadsheetId=spreadsheet_id, range=range_a1,
        ).execute()
    except Exception as exc:
        log.warning("Tab '%s' nicht lesbar: %s", tab, exc)
        return False

    values = result.get("values") or []
    target_row: int | None = None
    for idx, row in enumerate(values):
        if not row:
            continue
        if row[0] == account_name:
            target_row = idx + 1  # 1-basiert
            break

    if target_row is None:
        return False

    # Spalte C (Soll-Anzahl) + Spalte F (Soll-Saldo) einzeln updaten,
    # damit die Formel-Spalten D, E, G dazwischen unangetastet bleiben.
    sheets.write_values(
        spreadsheet_id,
        f"{tab}!{MONAT_COL_SOLL_ANZAHL_LETTER}{target_row}",
        [[soll_count]],
    )
    sheets.write_values(
        spreadsheet_id,
        f"{tab}!{MONAT_COL_SOLL_SALDO_LETTER}{target_row}",
        [[soll_balance]],
    )
    return True
