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
    return 1


if __name__ == "__main__":
    sys.exit(cli())
