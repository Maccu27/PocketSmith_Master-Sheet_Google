"""Orchestrator für den PDF-Parse-Workflow.

Crawlt Drive nach Kontoauszug-PDFs, schickt unverarbeitete an Claude, schreibt
Soll-Anzahl + Soll-Saldo in den richtigen Monatstab der Master-Sheet (sofern
das Jahr konfiguriert ist), trackt alles in einem separaten Tracking-Sheet.
"""

from __future__ import annotations

import calendar
import logging
import time
from datetime import date, datetime
from typing import Any

from .config import Settings
from .drive_client import DriveClient
from .pdf_extractor import ExtractionResult, PDFExtractor
from .pdf_tracker import PDFTracker, ParsedRecord, TrackedTransaction
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

    markers = settings.folder_markers
    log.info("Scanning Drive folder %s for PDFs under any of %s/...",
             settings.drive_finanzen_folder_id, markers)
    pdfs = drive.find_pdfs_under_marker(
        settings.drive_finanzen_folder_id,
        folder_marker=markers,
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

    for i, pdf in enumerate(new_pdfs):
        log.info("Verarbeite: %s (%.0f KB) [%d/%d]", pdf.path, pdf.size / 1024, i + 1, len(new_pdfs))
        try:
            pdf_bytes = drive.download_bytes(pdf.id)
            result = extractor.extract(pdf_bytes, accounts=accounts, pdf_filename=pdf.name)
        except Exception as exc:
            log.error("Fehler bei %s: %s", pdf.path, exc, exc_info=False)
            tracker.append_error(pdf.id, pdf.path, str(exc))
            counters["errors"] += 1
            # Bei Rate Limit kurz warten, sonst wird der nächste Call auch failen
            if "rate_limit" in str(exc).lower() or "429" in str(exc):
                log.info("  Rate-Limit getroffen — 30s pausieren")
                time.sleep(30)
            continue
        # Throttle: 2s zwischen Calls hält uns sicher unter 30k tokens/min
        # (mit Prompt Caching ist der Verbrauch eh viel niedriger).
        time.sleep(2)

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
        notes = result.notes or ""

        # Vier-Augen-Check: starting_balance(this) ?= ending_balance(prev)
        if matched and result.starting_balance:
            prev = _find_previous_record(tracker, matched.id, result.statement_period_start)
            if prev:
                diff = round(result.starting_balance - prev.ending_balance, 2)
                if abs(diff) > 0.01:
                    warning = (
                        f"⚠ Vier-Augen-Diff {diff:+.2f}: "
                        f"starting_balance ({result.starting_balance:.2f}) "
                        f"!= prev ending_balance ({prev.ending_balance:.2f}, {prev.path})"
                    )
                    notes = (notes + " | " + warning).strip(" |")
                    log.warning("  %s", warning)

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
            starting_balance=result.starting_balance,
            ending_balance=result.ending_balance,
            transaction_count=result.transaction_count,
            confidence=result.confidence,
            notes=notes,
            transactions=[
                TrackedTransaction(date=tx.date, amount=tx.amount, description=tx.description)
                for tx in result.transactions
            ],
        )
        tracker.append_parsed(record)
        log.info("  → %d Transaktionen extrahiert, Saldo %s %.2f",
                 record.transaction_count, record.currency, record.ending_balance)

    log.info("Phase 1 (Parse) fertig — Sheets werden in der Backfill-Phase aus dem Tracker befüllt")
    return counters


def _find_previous_record(
    tracker: PDFTracker,
    account_id: int,
    current_period_start: str,
) -> ParsedRecord | None:
    """Findet den jüngsten Record für ein Konto, dessen statement_period_end vor current_period_start liegt."""
    if not current_period_start:
        return None
    candidates = [
        r for r in tracker.all_parsed_records()
        if r.matched_account_id == account_id
        and r.statement_period_end
        and r.statement_period_end < current_period_start
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.statement_period_end)


def aggregate_pdf_data_for_month(
    records: list[ParsedRecord],
    *,
    account_id: int,
    year: int,
    month: int,
) -> tuple[int, float | None]:
    """Aggregiere Tracker-Daten zu Soll-Werten für (year, month, account).

    Liefert (soll_count, soll_balance_at_eom_or_None).

    PayPal-Spezialfall: wenn die Tx-Liste eines Records tx_type-Felder
    gesetzt hat, wird die paypal_classifier-Pipeline genutzt — dann zählen
    nur echte PayPal-Cashflow-Buchungen, Pass-Through-Käufe (per Bankkonto
    abgebucht) werden ignoriert.
    """
    from .paypal_classifier import classify_paypal_transactions, is_paypal_format

    last_day = date(year, month, calendar.monthrange(year, month)[1])
    first_day = date(year, month, 1)

    rec_for_account = [r for r in records if r.matched_account_id == account_id]

    soll_count = 0
    for r in rec_for_account:
        # Pro Record: ggf. PayPal-Klassifikation
        if is_paypal_format(r.transactions):
            classifications = classify_paypal_transactions(r.transactions)
            relevant_txs = [
                tx for i, tx in enumerate(r.transactions)
                if classifications.get(i) == "cashflow"
            ]
        else:
            relevant_txs = r.transactions

        for tx in relevant_txs:
            try:
                tx_date = date.fromisoformat(tx.date)
            except (ValueError, TypeError):
                continue
            if first_day <= tx_date <= last_day:
                soll_count += 1

    eom_record: ParsedRecord | None = None
    for r in rec_for_account:
        try:
            period_start = date.fromisoformat(r.statement_period_start)
            period_end = date.fromisoformat(r.statement_period_end)
        except (ValueError, TypeError):
            continue
        if period_start <= last_day <= period_end:
            eom_record = r
            break

    eom_balance: float | None
    if eom_record is None:
        eom_balance = None
    else:
        post_sum = 0.0
        # Bei PayPal: post-Tx-Summe aus dem Auszug ergibt nicht den korrekten
        # Saldo, weil viele Tx pass-through sind. Wir verlassen uns hier auf
        # ending_balance vom Auszug-Stichtag selbst — bei PayPal-Auszügen ist
        # der oft nicht gegeben (PDF zeigt keinen Saldo). Fallback: leer.
        if is_paypal_format(eom_record.transactions):
            # PayPal-PDF kennt keinen Saldo zuverlässig; auf None setzen
            eom_balance = None if eom_record.ending_balance == 0.0 else round(eom_record.ending_balance, 2)
        else:
            for tx in eom_record.transactions:
                try:
                    tx_date = date.fromisoformat(tx.date)
                except (ValueError, TypeError):
                    continue
                if tx_date > last_day:
                    post_sum += tx.amount
            eom_balance = round(eom_record.ending_balance - post_sum, 2)

    return soll_count, eom_balance


def backfill_master_sheet_from_tracker(
    settings: Settings,
    *,
    cred_info: dict[str, Any],
    year: int,
    _shared_tracker: PDFTracker | None = None,
    _shared_sheets: SheetsClient | None = None,
    _row_cache: dict[tuple[str, str], dict[str, int]] | None = None,
) -> int:
    """Schreibt für ein Jahr alle bereits getrackten Soll-Werte in die Master-Sheet.

    Wenn `_shared_tracker` und `_shared_sheets` mitgegeben werden, nutzt die
    Funktion sie statt neue Instanzen anzulegen — wichtig für Multi-Year-Runs,
    damit der Tracker-Read nicht 24× anfällt (Quota-Limit Sheets API).
    """
    sheets_per_year = settings.sheets_per_year
    master_sheet_id = sheets_per_year.get(year)
    if not master_sheet_id:
        raise RuntimeError(f"Kein MASTER_SHEET_{year} konfiguriert")

    sheets = _shared_sheets or SheetsClient(cred_info)
    if _shared_tracker is None:
        drive = DriveClient(cred_info)
        tracker = PDFTracker(
            sheets, drive,
            finanzen_folder_id=settings.drive_finanzen_folder_id or "",
            explicit_sheet_id=settings.pdf_tracking_sheet_id,
        )
    else:
        tracker = _shared_tracker

    records = tracker.all_parsed_records()  # gecacht in tracker

    # Sammle alle Konten, die in diesem Kalenderjahr mindestens 1 Tx hatten.
    # Verwende dafür die Tx-Liste aus den Tracker-Records (kalendergenau).
    account_names: dict[int, str] = {}
    for r in records:
        if not r.matched_account_id or not r.matched_account_name:
            continue
        for tx in r.transactions:
            try:
                tx_date = date.fromisoformat(tx.date)
            except (ValueError, TypeError):
                continue
            if tx_date.year == year:
                account_names[r.matched_account_id] = r.matched_account_name
                break

    log.info("Backfill für %d: %d Konten haben Tracker-Daten", year, len(account_names))

    if _row_cache is None:
        _row_cache = {}

    written = 0
    for account_id, account_name in account_names.items():
        for month in range(1, 13):
            soll_count, soll_balance = aggregate_pdf_data_for_month(
                records, account_id=account_id, year=year, month=month,
            )
            if soll_count == 0 and soll_balance is None:
                continue
            try:
                ok = write_soll_to_master_sheet(
                    sheets, master_sheet_id, year=year, month=month,
                    account_name=account_name,
                    soll_count=soll_count,
                    soll_balance=soll_balance,
                    _row_cache=_row_cache,
                )
                if ok:
                    written += 1
            except Exception as exc:
                log.warning("Backfill skip für %s/%d-%02d: %s", account_name, year, month, exc)
    return written


def backfill_all_configured_years(
    settings: Settings,
    *,
    cred_info: dict[str, Any],
) -> dict[int, int]:
    """Backfill-Lauf über alle Jahre, mit shared Tracker + Row-Cache.

    Spart Sheets-API-Reads: Tracker wird 1× geladen, Konten-Tab-Reads pro
    Master-Sheet/Tab werden gecacht.
    """
    sheets = SheetsClient(cred_info)
    drive = DriveClient(cred_info)
    tracker = PDFTracker(
        sheets, drive,
        finanzen_folder_id=settings.drive_finanzen_folder_id or "",
        explicit_sheet_id=settings.pdf_tracking_sheet_id,
    )
    # Lade Records einmal vorab (cached für alle weiteren Aufrufe)
    tracker.all_parsed_records()

    row_cache: dict[tuple[str, str], dict[str, int]] = {}
    results: dict[int, int] = {}

    for year in sorted(settings.sheets_per_year.keys()):
        try:
            written = backfill_master_sheet_from_tracker(
                settings, cred_info=cred_info, year=year,
                _shared_tracker=tracker,
                _shared_sheets=sheets,
                _row_cache=row_cache,
            )
            results[year] = written
            log.info("backfill %d: %d Soll-Werte geschrieben", year, written)
        except Exception as exc:
            log.error("backfill %d fehlgeschlagen: %s", year, exc)
            results[year] = -1
        # Kleine Pause gegen Sheets-API-Quota (60 Read/min Limit)
        time.sleep(1)

    return results


def write_soll_to_master_sheet(
    sheets: SheetsClient,
    spreadsheet_id: str,
    *,
    year: int,
    month: int,
    account_name: str,
    soll_count: int,
    soll_balance: float | None,
    _row_cache: dict[tuple[str, str], dict[str, int]] | None = None,
) -> bool:
    """Findet die Zeile mit `account_name` im Monatstab und schreibt Soll-Werte.

    Returns True wenn geschrieben wurde, False wenn der Account in der Konten-Tab
    nicht aktiv ist (d. h. die Zeile fehlt in dem Monatstab).

    Mit `_row_cache` wird der Konto→Zeile-Lookup pro (sheet, tab) gecacht —
    das spart bei Multi-Backfill viele Sheets-API-Read-Calls (Quota!).
    """
    tab = monat_tab_name(year, month)
    cache_key = (spreadsheet_id, tab)

    if _row_cache is not None and cache_key in _row_cache:
        row_map = _row_cache[cache_key]
    else:
        range_a1 = f"{tab}!A1:A300"
        try:
            result = sheets._sheets.spreadsheets().values().get(  # noqa: SLF001
                spreadsheetId=spreadsheet_id, range=range_a1,
            ).execute()
        except Exception as exc:
            log.warning("Tab '%s' nicht lesbar: %s", tab, exc)
            if _row_cache is not None:
                _row_cache[cache_key] = {}
            return False
        values = result.get("values") or []
        row_map = {
            row[0]: idx + 1
            for idx, row in enumerate(values)
            if row and row[0]
        }
        if _row_cache is not None:
            _row_cache[cache_key] = row_map

    target_row = row_map.get(account_name)
    if target_row is None:
        return False

    # Spalte C (Soll-Anzahl) und Spalte F (Soll-Saldo) einzeln updaten,
    # damit Formel-Spalten dazwischen unangetastet bleiben.
    sheets.write_values(
        spreadsheet_id,
        f"{tab}!{MONAT_COL_SOLL_ANZAHL_LETTER}{target_row}",
        [[soll_count]],
    )
    if soll_balance is not None:
        sheets.write_values(
            spreadsheet_id,
            f"{tab}!{MONAT_COL_SOLL_SALDO_LETTER}{target_row}",
            [[soll_balance]],
        )
    return True
