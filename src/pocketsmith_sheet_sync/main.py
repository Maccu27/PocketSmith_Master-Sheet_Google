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


def cli() -> int:
    parser = argparse.ArgumentParser(prog="pocketsmith-sync")
    parser.add_argument("command", choices=["sync"], help="action to run")
    parser.add_argument(
        "--years",
        help="comma-separated list of years to sync (overrides SYNC_YEARS env)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)

    settings = load_settings()
    if args.years:
        years = [int(y.strip()) for y in args.years.split(",") if y.strip()]
    else:
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

    log.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(cli())
