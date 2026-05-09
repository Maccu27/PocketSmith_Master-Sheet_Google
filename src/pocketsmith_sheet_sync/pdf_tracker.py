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
    "file_id",                  # 0  A
    "path",                     # 1  B
    "parsed_at",                # 2  C
    "bank_name",                # 3  D
    "iban_or_account_number",   # 4  E
    "matched_account_id",       # 5  F
    "matched_account_name",     # 6  G
    "currency",                 # 7  H
    "year",                     # 8  I  Stichtag-Jahr (Zuordnung Auszug)
    "month",                    # 9  J  Stichtag-Monat
    "statement_period_start",   # 10 K
    "statement_period_end",     # 11 L
    "starting_balance",         # 12 M  NEU
    "ending_balance",           # 13 N
    "transaction_count",        # 14 O
    "confidence",               # 15 P
    "notes",                    # 16 Q
    "transactions_json",        # 17 R  NEU – JSON-Array aller Tx
]
ERROR_HEADERS = ["file_id", "path", "parsed_at", "error_message"]


@dataclass(frozen=True)
class TrackedTransaction:
    date: str
    amount: float
    description: str
    tx_type: str = ""
    status: str = ""


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
    starting_balance: float
    ending_balance: float
    transaction_count: int
    confidence: float
    notes: str
    transactions: list[TrackedTransaction]

    def as_row(self) -> list[Any]:
        import json as _json
        tx_payload = _json.dumps(
            [
                {
                    "date": t.date,
                    "amount": t.amount,
                    "description": t.description,
                    "tx_type": t.tx_type,
                    "status": t.status,
                }
                for t in self.transactions
            ],
            ensure_ascii=False,
        )
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
            self.starting_balance,
            self.ending_balance,
            self.transaction_count,
            self.confidence,
            self.notes,
            tx_payload,
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
        self._tabs_initialized = False

    @property
    def sheet_id(self) -> str:
        if not self._sheet_id:
            existing = self._drive.find_in_folder_by_name(
                self._finanzen_folder_id, TRACKING_SHEET_NAME, mime_type=SPREADSHEET_MIME,
            )
            if existing:
                self._sheet_id = existing
            else:
                log.info("Tracking-Sheet existiert nicht, lege es an in Finanzen-Ordner")
                self._sheet_id = self._drive.create_spreadsheet_in_folder(
                    self._finanzen_folder_id, TRACKING_SHEET_NAME,
                )
        # Tabs/Header sind idempotent — auch bei manuell angelegtem Sheet
        # stellen wir sicher, dass die richtigen Tabs + Header da sind.
        if not self._tabs_initialized:
            self._initialize_tabs()
            self._tabs_initialized = True
        return self._sheet_id

    def _initialize_tabs(self) -> None:
        sheet_id = self._sheet_id
        assert sheet_id
        self._sheets.ensure_tab(sheet_id, PARSED_TAB, index=0, rows=2000, cols=20)
        self._sheets.ensure_tab(sheet_id, ERROR_TAB, index=1, rows=500, cols=10)
        # Headers schreiben — Tab-Namen mit Leerzeichen brauchen Quotes
        self._sheets.write_values(sheet_id, f"'{PARSED_TAB}'!A1", [PARSED_HEADERS])
        self._sheets.write_values(sheet_id, f"'{ERROR_TAB}'!A1", [ERROR_HEADERS])
        # Default-Tab löschen
        self._sheets.delete_default_blank_tab(sheet_id)

    def processed_file_ids(self) -> set[str]:
        if self._processed_ids is not None:
            return self._processed_ids
        sheet_id = self.sheet_id
        try:
            result = self._sheets._sheets.spreadsheets().values().get(  # noqa: SLF001
                spreadsheetId=sheet_id, range=f"'{PARSED_TAB}'!A2:A",
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
        # Tab-Name mit Leerzeichen → in single quotes; explicit A1:R Range.
        self._sheets._sheets.spreadsheets().values().append(  # noqa: SLF001
            spreadsheetId=sheet_id,
            range=f"'{PARSED_TAB}'!A1:R",
            valueInputOption="USER_ENTERED",
            body=body,
        ).execute()
        if self._processed_ids is not None:
            self._processed_ids.add(record.file_id)
        # Cache invalidieren — wir haben einen neuen Record geschrieben.
        if hasattr(self, "_records_cache"):
            del self._records_cache  # type: ignore[attr-defined]

    def reset_data(self) -> None:
        """Löscht alle Datenzeilen (außer Header) im Tracking-Sheet.

        Wird aufgerufen wenn das Schema geändert wird und alle PDFs neu
        verarbeitet werden müssen — dadurch findet der nächste Parse-Run
        alle PDFs als 'unverarbeitet'.
        """
        sheet_id = self.sheet_id
        # Datenzeilen ab Zeile 2 leeren
        self._sheets.clear_range(sheet_id, f"'{PARSED_TAB}'!A2:Z10000")
        self._sheets.clear_range(sheet_id, f"'{ERROR_TAB}'!A2:Z10000")
        # Header neu schreiben (falls Schema sich geändert hat)
        self._sheets.write_values(sheet_id, f"'{PARSED_TAB}'!A1", [PARSED_HEADERS])
        self._sheets.write_values(sheet_id, f"'{ERROR_TAB}'!A1", [ERROR_HEADERS])
        self._processed_ids = set()
        if hasattr(self, "_records_cache"):
            del self._records_cache  # type: ignore[attr-defined]
        log.info("Tracking-Sheet zurückgesetzt — alle PDFs werden beim nächsten Run neu verarbeitet")

    def append_error(self, file_id: str, path: str, error: str) -> None:
        sheet_id = self.sheet_id
        body = {"values": [[file_id, path, datetime.utcnow().isoformat(), error]]}
        self._sheets._sheets.spreadsheets().values().append(  # noqa: SLF001
            spreadsheetId=sheet_id,
            range=f"'{ERROR_TAB}'!A1:D",
            valueInputOption="USER_ENTERED",
            body=body,
        ).execute()

    def all_parsed_records(self) -> list[ParsedRecord]:
        """Lade alle verarbeiteten PDFs für den Backfill in Master-Sheets.

        Cached pro Tracker-Instanz — wenn der gleiche Run mehrmals fragt,
        wird nicht jedes Mal die ganze Tracker-Sheet neu gelesen (Quota!).
        """
        if hasattr(self, "_records_cache"):
            return self._records_cache  # type: ignore[attr-defined]

        sheet_id = self.sheet_id
        try:
            result = self._sheets._sheets.spreadsheets().values().get(  # noqa: SLF001
                spreadsheetId=sheet_id, range=f"'{PARSED_TAB}'!A2:R",
            ).execute()
        except Exception:
            self._records_cache = []  # type: ignore[attr-defined]
            return []
        rows = result.get("values") or []
        records: list[ParsedRecord] = []
        import json as _json
        for row in rows:
            if not row or not row[0]:
                continue
            row = (row + [""] * 18)[:18]
            try:
                tx_payload = row[17]
                tracked_txs: list[TrackedTransaction] = []
                if tx_payload:
                    try:
                        for raw in _json.loads(tx_payload):
                            tracked_txs.append(TrackedTransaction(
                                date=str(raw.get("date", "")),
                                amount=float(raw.get("amount", 0.0) or 0.0),
                                description=str(raw.get("description", "")),
                                tx_type=str(raw.get("tx_type", "")),
                                status=str(raw.get("status", "")),
                            ))
                    except (ValueError, TypeError) as parse_exc:
                        log.warning("transactions_json malformed for %s: %s", row[0], parse_exc)
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
                    starting_balance=float(str(row[12]).replace(",", ".")) if row[12] else 0.0,
                    ending_balance=float(str(row[13]).replace(",", ".")) if row[13] else 0.0,
                    transaction_count=int(row[14]) if row[14] else 0,
                    confidence=float(str(row[15]).replace(",", ".")) if row[15] else 0.0,
                    notes=row[16],
                    transactions=tracked_txs,
                ))
            except (ValueError, IndexError) as exc:
                log.warning("Skipping malformed tracker row: %s (%s)", row, exc)
        self._records_cache = records  # type: ignore[attr-defined]
        return records
