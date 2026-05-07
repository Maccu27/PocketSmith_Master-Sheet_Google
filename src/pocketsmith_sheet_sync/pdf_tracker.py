"""Tracking-Sheet: speichert verarbeitete PDFs damit Cron nicht alles neu macht.

Lebt als eigene Google Sheet im Finanzen-Ordner. Wird beim ersten Run automatisch
erstellt, falls nicht vorhanden. ID kann optional über PDF_TRACKING_SHEET_ID
fixiert werden.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .drive_client import DriveClient, SPREADSHEET_MIME
from .pdf_extractor import ExtractionResult
from .sheets import SheetsClient

log = logging.getLogger(__name__)

TRACKING_SHEET_NAME = "PocketSmith PDF Tracking"
PARSED_TAB = "Verarbeitete PDFs"
ERROR_TAB = "Fehler-Log"

PARSED_HEADERS = [
    "file_id",
    "path",
    "parsed_at",
    "bank_name",
    "iban_or_account_number",
    "matched_account_id",
    "matched_account_name",
    "currency",
    "year",
    "month",
    "statement_period_start",
    "statement_period_end",
    "transaction_count",
    "ending_balance",
    "confidence",
    "notes",
]
ERROR_HEADERS = ["file_id", "path", "parsed_at", "error_message"]


@dataclass(frozen=True)
class ParsedRecord:
    file_id: str
    path: str
    parsed_at: str
    bank_name: str
    iban_or_account_number: str
    matched_account_id: int | None
    matched_account_name: str
    currency: str
    year: int
    month: int
    statement_period_start: str
    statement_period_end: str
    transaction_count: int
    ending_balance: float
    confidence: float
    notes: str

    def as_row(self) -> list[Any]:
        return [
            self.file_id,
            self.path,
            self.parsed_at,
            self.bank_name,
            self.iban_or_account_number,
            self.matched_account_id if self.matched_account_id is not None else "",
            self.matched_account_name,
            self.currency,
            self.year,
            self.month,
            self.statement_period_start,
            self.statement_period_end,
            self.transaction_count,
            self.ending_balance,
            self.confidence,
            self.notes,
        ]


class PDFTracker:
    def __init__(
        self,
        sheets: SheetsClient,
        drive: DriveClient,
        *,
        finanzen_folder_id: str,
        explicit_sheet_id: str | None = None,
    ):
        self._sheets = sheets
        self._drive = drive
        self._finanzen_folder_id = finanzen_folder_id
        self._sheet_id = explicit_sheet_id
        self._processed_ids: set[str] | None = None

    @property
    def sheet_id(self) -> str:
        if self._sheet_id:
            return self._sheet_id
        # Suche Sheet im Finanzen-Ordner
        existing = self._drive.find_in_folder_by_name(
            self._finanzen_folder_id, TRACKING_SHEET_NAME, mime_type=SPREADSHEET_MIME,
        )
        if existing:
            self._sheet_id = existing
            return existing
        # Erstellen
        log.info("Tracking-Sheet existiert nicht, lege es an in Finanzen-Ordner")
        new_id = self._drive.create_spreadsheet_in_folder(
            self._finanzen_folder_id, TRACKING_SHEET_NAME,
        )
        self._sheet_id = new_id
        self._initialize_tabs()
        return new_id

    def _initialize_tabs(self) -> None:
        sheet_id = self._sheet_id
        assert sheet_id
        self._sheets.ensure_tab(sheet_id, PARSED_TAB, index=0, rows=2000, cols=20)
        self._sheets.ensure_tab(sheet_id, ERROR_TAB, index=1, rows=500, cols=10)
        # Headers schreiben
        self._sheets.write_values(sheet_id, f"{PARSED_TAB}!A1", [PARSED_HEADERS])
        self._sheets.write_values(sheet_id, f"{ERROR_TAB}!A1", [ERROR_HEADERS])
        # Default-Tab löschen
        self._sheets.delete_default_blank_tab(sheet_id)

    def processed_file_ids(self) -> set[str]:
        if self._processed_ids is not None:
            return self._processed_ids
        sheet_id = self.sheet_id
        try:
            result = self._sheets._sheets.spreadsheets().values().get(  # noqa: SLF001
                spreadsheetId=sheet_id, range=f"{PARSED_TAB}!A2:A",
            ).execute()
            rows = result.get("values") or []
            self._processed_ids = {row[0] for row in rows if row and row[0]}
        except Exception:
            # Tab existiert noch nicht oder Fehler — bei nächstem Run neu probieren
            self._processed_ids = set()
        return self._processed_ids

    def append_parsed(self, record: ParsedRecord) -> None:
        sheet_id = self.sheet_id
        body = {"values": [record.as_row()]}
        self._sheets._sheets.spreadsheets().values().append(  # noqa: SLF001
            spreadsheetId=sheet_id,
            range=f"{PARSED_TAB}!A:Z",
            valueInputOption="USER_ENTERED",
            body=body,
        ).execute()
        if self._processed_ids is not None:
            self._processed_ids.add(record.file_id)

    def append_error(self, file_id: str, path: str, error: str) -> None:
        sheet_id = self.sheet_id
        body = {"values": [[file_id, path, datetime.utcnow().isoformat(), error]]}
        self._sheets._sheets.spreadsheets().values().append(  # noqa: SLF001
            spreadsheetId=sheet_id,
            range=f"{ERROR_TAB}!A:D",
            valueInputOption="USER_ENTERED",
            body=body,
        ).execute()

    def all_parsed_records(self) -> list[ParsedRecord]:
        """Lade alle verarbeiteten PDFs für den Backfill in Master-Sheets."""
        sheet_id = self.sheet_id
        try:
            result = self._sheets._sheets.spreadsheets().values().get(  # noqa: SLF001
                spreadsheetId=sheet_id, range=f"{PARSED_TAB}!A2:P",
            ).execute()
        except Exception:
            return []
        rows = result.get("values") or []
        records: list[ParsedRecord] = []
        for row in rows:
            if not row or not row[0]:
                continue
            row = (row + [""] * 16)[:16]
            try:
                records.append(ParsedRecord(
                    file_id=row[0],
                    path=row[1],
                    parsed_at=row[2],
                    bank_name=row[3],
                    iban_or_account_number=row[4],
                    matched_account_id=int(row[5]) if row[5] else None,
                    matched_account_name=row[6],
                    currency=row[7],
                    year=int(row[8]) if row[8] else 0,
                    month=int(row[9]) if row[9] else 0,
                    statement_period_start=row[10],
                    statement_period_end=row[11],
                    transaction_count=int(row[12]) if row[12] else 0,
                    ending_balance=float(str(row[13]).replace(",", ".")) if row[13] else 0.0,
                    confidence=float(str(row[14]).replace(",", ".")) if row[14] else 0.0,
                    notes=row[15],
                ))
            except (ValueError, IndexError) as exc:
                log.warning("Skipping malformed tracker row: %s (%s)", row, exc)
        return records
