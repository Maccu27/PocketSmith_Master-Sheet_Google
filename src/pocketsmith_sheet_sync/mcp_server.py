"""Lokaler MCP-Server zum manuellen Triggern des PocketSmith-Sheet-Syncs.

Wird über Claude Code als stdio-MCP-Server eingebunden. Tools:
  - sync_year(year): synchronisiert ein Jahr (synchron, dauert ~7 Min für 2026)
  - list_years(): zeigt die konfigurierten Jahre
  - health_check(): prüft, ob alle Variablen + Sheet-Zugriff stimmen
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import load_settings
from .pocketsmith import PocketSmithClient
from .sheets import SheetsClient
from .sync import sync_year as _sync_year_impl

log = logging.getLogger(__name__)

mcp = FastMCP("pocketsmith-sheet-sync")


@mcp.tool()
def sync_year(year: int = 2026) -> str:
    """
    Triggert einen kompletten Sync für ein Jahr in die zugehörige Master-Sheet.

    Lädt alle Konten aus PocketSmith, aggregiert Transaktionen, schreibt
    Übersicht/Konten/Monatstabs/Anleitung in das konfigurierte Google Sheet.
    Dauer ca. 5–10 Min für ein volles Jahr (~50 aktive Konten).

    Voraussetzung: Jahr muss in SYNC_YEARS und MASTER_SHEET_<JAHR> in der
    .env oder Umgebungs-Variablen konfiguriert sein.
    """
    settings = load_settings()
    sheet_id = settings.sheets_per_year.get(year)
    if not sheet_id:
        return (
            f"Kein Sheet für {year} konfiguriert. Setze MASTER_SHEET_{year} und füge "
            f"{year} zu SYNC_YEARS hinzu. Konfigurierte Jahre: {settings.years}"
        )

    today = date.today()
    cred_info = settings.google_credentials_info()
    gs = SheetsClient(cred_info)

    with PocketSmithClient(settings.pocketsmith_api_key) as ps:
        _sync_year_impl(
            ps,
            gs,
            spreadsheet_id=sheet_id,
            year=year,
            today=today,
            verified_label=settings.verified_label,
        )

    return f"Sync für {year} erfolgreich. Sheet-ID: {sheet_id}"


@mcp.tool()
def list_years() -> dict[str, Any]:
    """Zeigt die aktuell konfigurierten Jahre und zugehörigen Sheet-IDs."""
    settings = load_settings()
    return {
        "configured_years": settings.years,
        "sheets_per_year": settings.sheets_per_year,
        "verified_label": settings.verified_label,
    }


@mcp.tool()
def parse_pdfs() -> dict[str, Any]:
    """
    Triggert den PDF-Parser: scannt Drive-Ordner Finanzen/.../Kontoauszüge,
    schickt neue PDFs an Claude, schreibt Soll-Werte in Master-Sheets.

    Voraussetzungen:
      - ANTHROPIC_API_KEY in .env / Railway
      - DRIVE_FINANZEN_FOLDER_ID auf Wurzel-Drive-Ordner

    Bei vielen unverarbeiteten PDFs kann es lange dauern (~5–15 s pro PDF).
    """
    from .pdf_sync import parse_all_new_pdfs
    settings = load_settings()
    cred_info = settings.google_credentials_info()
    return parse_all_new_pdfs(settings, cred_info=cred_info, today=date.today())


@mcp.tool()
def backfill_year(year: int) -> dict[str, Any]:
    """Schreibt für ein bestimmtes Jahr alle bereits getrackten Soll-Werte in
    die zugehörige Master-Sheet (z. B. nach dem Anlegen einer neuen Jahres-Sheet).
    """
    from .pdf_sync import backfill_master_sheet_from_tracker
    settings = load_settings()
    cred_info = settings.google_credentials_info()
    written = backfill_master_sheet_from_tracker(settings, cred_info=cred_info, year=year)
    return {"year": year, "rows_written": written}


@mcp.tool()
def health_check() -> dict[str, Any]:
    """
    Prüft, ob das Setup funktional ist:
      - Env-Variablen vorhanden
      - PocketSmith API erreichbar
      - Service Account hat Zugriff auf alle konfigurierten Sheets
    """
    result: dict[str, Any] = {"ok": True, "checks": {}, "issues": []}

    try:
        settings = load_settings()
        result["checks"]["env_loaded"] = True
    except Exception as exc:
        result["ok"] = False
        result["checks"]["env_loaded"] = False
        result["issues"].append(f"Env-Settings konnten nicht geladen werden: {exc}")
        return result

    # PocketSmith
    try:
        with PocketSmithClient(settings.pocketsmith_api_key) as ps:
            uid = ps.user_id()
            accounts = ps.list_accounts()
            result["checks"]["pocketsmith_user_id"] = uid
            result["checks"]["pocketsmith_account_count"] = len(accounts)
    except Exception as exc:
        result["ok"] = False
        result["checks"]["pocketsmith"] = False
        result["issues"].append(f"PocketSmith-API-Fehler: {exc}")

    # Google Sheets
    try:
        cred_info = settings.google_credentials_info()
        gs = SheetsClient(cred_info)
        sheet_access: dict[int, str] = {}
        for year, sheet_id in settings.sheets_per_year.items():
            try:
                meta = gs.get_metadata(sheet_id)
                sheet_access[year] = f"OK – {meta['properties']['title']}"
            except Exception as exc:
                sheet_access[year] = f"FEHLER: {exc}"
                result["ok"] = False
                result["issues"].append(f"Sheet {year} nicht erreichbar: {exc}")
        result["checks"]["sheets"] = sheet_access
    except Exception as exc:
        result["ok"] = False
        result["checks"]["google"] = False
        result["issues"].append(f"Google-Auth-Fehler: {exc}")

    return result


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    mcp.run()


if __name__ == "__main__":
    main()
