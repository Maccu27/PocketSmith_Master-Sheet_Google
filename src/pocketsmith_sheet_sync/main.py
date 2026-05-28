from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from .config import load_settings
from .pocketsmith import PocketSmithClient
from .sheets import SheetsClient
from .sync import sync_year

log = logging.getLogger("pocketsmith_sheet_sync")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _run_sync(years: list[int] | None, verbose: bool) -> int:
    settings = load_settings()
    if years is None:
        years = settings.years

    sheets_per_year = settings.sheets_per_year
    if not sheets_per_year:
        log.error("No master sheet IDs configured. Set MASTER_SHEET_<YEAR> env vars.")
        return 2

    today = date.today()
    cred_info = settings.google_credentials_info()
    gs = SheetsClient(cred_info)

    with PocketSmithClient(settings.pocketsmith_api_key) as ps:
        for year in years:
            sheet_id = sheets_per_year.get(year)
            if not sheet_id:
                log.warning("year %d has no MASTER_SHEET_%d configured, skipping", year, year)
                continue
            log.info("=== syncing %d into sheet %s ===", year, sheet_id)
            sync_year(
                ps,
                gs,
                spreadsheet_id=sheet_id,
                year=year,
                today=today,
                verified_label=settings.verified_label,
            )

    log.info("sync done")
    return 0


def _run_parse_pdfs(verbose: bool) -> int:
    from .pdf_sync import parse_all_new_pdfs
    settings = load_settings()
    today = date.today()
    cred_info = settings.google_credentials_info()
    counters = parse_all_new_pdfs(settings, cred_info=cred_info, today=today)
    log.info("parse-pdfs done: %s", counters)
    return 0


def _run_backfill(year: int, verbose: bool) -> int:
    from .pdf_sync import backfill_master_sheet_from_tracker
    settings = load_settings()
    cred_info = settings.google_credentials_info()
    written = backfill_master_sheet_from_tracker(settings, cred_info=cred_info, year=year)
    log.info("backfill %d done: %d Zeilen geschrieben", year, written)
    return 0


def _run_reset_tracker(verbose: bool) -> int:
    """Löscht alle PDF-Records aus dem Tracking-Sheet, damit alle PDFs neu prozessiert werden."""
    from .drive_client import DriveClient
    from .pdf_tracker import PDFTracker
    settings = load_settings()
    if not settings.drive_finanzen_folder_id:
        log.error("DRIVE_FINANZEN_FOLDER_ID nicht gesetzt")
        return 2
    cred_info = settings.google_credentials_info()
    sheets = SheetsClient(cred_info)
    drive = DriveClient(cred_info)
    tracker = PDFTracker(
        sheets, drive,
        finanzen_folder_id=settings.drive_finanzen_folder_id,
        explicit_sheet_id=settings.pdf_tracking_sheet_id,
    )
    tracker.reset_data()
    log.info("Tracking-Sheet zurückgesetzt. Beim nächsten parse-pdfs werden alle PDFs neu verarbeitet.")
    return 0


def _run_daily(verbose: bool) -> int:
    """Combine: PocketSmith → Sheets, parse new PDFs, then backfill Soll-Werte."""
    log.info("====== daily run: phase 1 = PocketSmith sync ======")
    sync_rc = _run_sync(None, verbose)
    if sync_rc != 0:
        log.error("PocketSmith sync exited with rc=%d, fortsetzen mit parse-pdfs", sync_rc)

    log.info("====== daily run: phase 2 = PDF parser ======")
    try:
        _run_parse_pdfs(verbose)
    except Exception as exc:
        log.error("parse-pdfs schlug fehl: %s", exc, exc_info=True)

    log.info("====== daily run: phase 3 = backfill Soll-Werte ins Master-Sheets ======")
    # Backfill für alle konfigurierten Jahre — gemeinsamer Tracker + Row-Cache,
    # damit wir nicht ins 60/min Read-Limit der Sheets-API laufen.
    try:
        from .pdf_sync import backfill_all_configured_years
        settings = load_settings()
        cred_info = settings.google_credentials_info()
        results = backfill_all_configured_years(settings, cred_info=cred_info)
        log.info("backfill-summary: %s", results)
    except Exception as exc:
        log.error("backfill-phase schlug fehl: %s", exc, exc_info=True)

    log.info("====== daily run done ======")
    return 0


def _run_parse_and_backfill(verbose: bool) -> int:
    """Wie daily, aber ohne Phase 1 (PocketSmith → Sheets).

    Sinnvoll bei großem Backfill über viele Jahre, wenn Phase 1 ins
    Google-Sheets-API-Rate-Limit (60 writes/min) läuft. PS-Sync ist
    für Backfill nicht zwingend nötig, weil Master-Sheets für ältere
    Jahre vermutlich eh statisch sind.
    """
    log.info("====== parse-and-backfill: phase 2 = PDF parser ======")
    try:
        _run_parse_pdfs(verbose)
    except Exception as exc:
        log.error("parse-pdfs schlug fehl: %s", exc, exc_info=True)

    log.info("====== parse-and-backfill: phase 3 = backfill Soll-Werte ======")
    try:
        from .pdf_sync import backfill_all_configured_years
        settings = load_settings()
        cred_info = settings.google_credentials_info()
        results = backfill_all_configured_years(settings, cred_info=cred_info)
        log.info("backfill-summary: %s", results)
    except Exception as exc:
        log.error("backfill-phase schlug fehl: %s", exc, exc_info=True)

    log.info("====== parse-and-backfill done ======")
    return 0


def cli() -> int:
    parser = argparse.ArgumentParser(prog="pocketsmith-sync")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="PocketSmith → Google Sheets")
    p_sync.add_argument("--years", help="comma-separated list of years")
    p_sync.add_argument("-v", "--verbose", action="store_true")

    p_pdf = sub.add_parser("parse-pdfs", help="Drive-PDFs → Soll-Werte in Sheets")
    p_pdf.add_argument("-v", "--verbose", action="store_true")

    p_bf = sub.add_parser("backfill", help="Tracker-Daten in eine Master-Sheet schreiben")
    p_bf.add_argument("--year", type=int, required=True)
    p_bf.add_argument("-v", "--verbose", action="store_true")

    p_daily = sub.add_parser("daily", help="sync + parse-pdfs + backfill in einem Lauf")
    p_daily.add_argument("-v", "--verbose", action="store_true")

    p_pab = sub.add_parser("parse-and-backfill", help="parse-pdfs + backfill (ohne PS-Sync) — für große Backfill-Runs")
    p_pab.add_argument("-v", "--verbose", action="store_true")

    p_reset = sub.add_parser("reset-tracker", help="ACHTUNG: löscht alle PDF-Records im Tracker")
    p_reset.add_argument("--confirm", action="store_true", required=True,
                         help="Bestätigung erforderlich, sonst keine Aktion")
    p_reset.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    setup_logging(getattr(args, "verbose", False))

    if args.command == "sync":
        years_arg = getattr(args, "years", None)
        years = [int(y.strip()) for y in years_arg.split(",")] if years_arg else None
        return _run_sync(years, args.verbose)
    if args.command == "parse-pdfs":
        return _run_parse_pdfs(args.verbose)
    if args.command == "backfill":
        return _run_backfill(args.year, args.verbose)
    if args.command == "daily":
        return _run_daily(args.verbose)
    if args.command == "parse-and-backfill":
        return _run_parse_and_backfill(args.verbose)
    if args.command == "reset-tracker":
        if not args.confirm:
            log.error("--confirm fehlt. Diese Aktion löscht alle PDF-Records.")
            return 2
        return _run_reset_tracker(args.verbose)
    return 1


if __name__ == "__main__":
    sys.exit(cli())
