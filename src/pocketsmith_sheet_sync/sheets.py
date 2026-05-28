from __future__ import annotations

import logging
import time
from typing import Any, Callable, TypeVar

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

T = TypeVar("T")


def _retry_on_rate_limit(fn: Callable[[], T], *, max_retries: int = 5, base_delay: float = 2.0) -> T:
    """Wrapper für Sheets-API-Calls mit exponential backoff bei HTTP 429.

    Google Sheets erlaubt nur 60 writes/min/user. Bei Bulk-Operationen (z.B.
    Multi-Year-Sync) wird das Limit gerne überschritten. Statt sofortigem
    Crash retryen wir 5× mit wachsendem Sleep (2s, 4s, 8s, 16s, 32s).
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except HttpError as exc:
            if exc.resp.status != 429:
                raise
            last_exc = exc
            wait = base_delay * (2 ** attempt)
            log.warning(
                "Sheets-API HTTP 429 (rate limit) — retry %d/%d in %.0fs",
                attempt + 1, max_retries, wait,
            )
            time.sleep(wait)
    # Alle Retries verbraucht → letzten Fehler propagieren
    assert last_exc is not None
    raise last_exc

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class SheetsClient:
    def __init__(self, credentials_info: dict[str, Any]):
        creds = Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
        self._sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        # Cached spreadsheet metadata pro Sheet-ID. Wird beim Hinzufügen oder
        # Löschen eines Tabs lokal aktualisiert; verhindert dutzende redundante
        # Reads pro Sync-Lauf (Hauptursache für Sync-Latenz und Quota-Hits).
        self._metadata_cache: dict[str, dict[str, Any]] = {}

    def get_metadata(self, spreadsheet_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
        if force_refresh or spreadsheet_id not in self._metadata_cache:
            self._metadata_cache[spreadsheet_id] = (
                self._sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
            )
        return self._metadata_cache[spreadsheet_id]

    def invalidate_metadata(self, spreadsheet_id: str) -> None:
        self._metadata_cache.pop(spreadsheet_id, None)

    def list_tabs(self, spreadsheet_id: str) -> dict[str, int]:
        meta = self.get_metadata(spreadsheet_id)
        return {
            sheet["properties"]["title"]: sheet["properties"]["sheetId"]
            for sheet in meta.get("sheets", [])
        }

    def ensure_tab(
        self,
        spreadsheet_id: str,
        title: str,
        *,
        index: int | None = None,
        rows: int = 200,
        cols: int = 20,
    ) -> int:
        tabs = self.list_tabs(spreadsheet_id)
        if title in tabs:
            return tabs[title]
        properties: dict[str, Any] = {
            "title": title,
            "gridProperties": {"rowCount": rows, "columnCount": cols},
        }
        if index is not None:
            properties["index"] = index
        request = {"addSheet": {"properties": properties}}
        response = self.batch_update(spreadsheet_id, [request])
        new_sheet_id = int(response["replies"][0]["addSheet"]["properties"]["sheetId"])
        # Cache lokal aktualisieren statt neu zu laden
        meta = self._metadata_cache.get(spreadsheet_id)
        if meta is not None:
            meta.setdefault("sheets", []).append(
                {
                    "properties": {
                        "title": title,
                        "sheetId": new_sheet_id,
                        "gridProperties": {"rowCount": rows, "columnCount": cols},
                    }
                }
            )
        return new_sheet_id

    def delete_default_blank_tab(self, spreadsheet_id: str) -> None:
        """Drop the default 'Tabellenblatt1' / 'Sheet1' if it's still empty and other tabs exist."""
        meta = self.get_metadata(spreadsheet_id)
        sheets = meta.get("sheets", [])
        if len(sheets) <= 1:
            return
        for sheet in sheets:
            props = sheet["properties"]
            if props["title"] in ("Tabellenblatt1", "Sheet1"):
                try:
                    self.batch_update(
                        spreadsheet_id,
                        [{"deleteSheet": {"sheetId": props["sheetId"]}}],
                    )
                    # Cache: gelöschten Tab entfernen
                    cached = self._metadata_cache.get(spreadsheet_id)
                    if cached is not None:
                        cached["sheets"] = [
                            s for s in cached.get("sheets", [])
                            if s["properties"]["sheetId"] != props["sheetId"]
                        ]
                except HttpError:
                    pass
                return

    def write_values(
        self,
        spreadsheet_id: str,
        range_a1: str,
        values: list[list[Any]],
        *,
        value_input_option: str = "USER_ENTERED",
    ) -> None:
        body = {"values": values}
        _retry_on_rate_limit(
            lambda: self._sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=range_a1,
                valueInputOption=value_input_option,
                body=body,
            ).execute()
        )

    def clear_range(self, spreadsheet_id: str, range_a1: str) -> None:
        _retry_on_rate_limit(
            lambda: self._sheets.spreadsheets().values().clear(
                spreadsheetId=spreadsheet_id, range=range_a1, body={}
            ).execute()
        )

    def batch_update(self, spreadsheet_id: str, requests: list[dict[str, Any]]) -> dict[str, Any]:
        if not requests:
            return {"replies": []}
        return _retry_on_rate_limit(
            lambda: self._sheets.spreadsheets()
            .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests})
            .execute()
        )

    def clear_protections_and_conditional_formats(
        self, spreadsheet_id: str, sheet_id: int
    ) -> None:
        """Wipe all protected ranges and conditional format rules for a tab."""
        meta = self.get_metadata(spreadsheet_id)
        target = next(
            (s for s in meta.get("sheets", []) if s["properties"]["sheetId"] == sheet_id),
            None,
        )
        if not target:
            return
        requests: list[dict[str, Any]] = []
        for prot in target.get("protectedRanges", []) or []:
            requests.append({"deleteProtectedRange": {"protectedRangeId": prot["protectedRangeId"]}})
        cf_count = len(target.get("conditionalFormats", []) or [])
        for i in reversed(range(cf_count)):
            requests.append({"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}})
        if requests:
            self.batch_update(spreadsheet_id, requests)
        # Cache: dieser Tab hat jetzt keine Protections/Conditional Formats mehr
        target["protectedRanges"] = []
        target["conditionalFormats"] = []
