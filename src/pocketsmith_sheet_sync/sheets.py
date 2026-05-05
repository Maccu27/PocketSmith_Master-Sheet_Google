from __future__ import annotations

import logging
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class SheetsClient:
    def __init__(self, credentials_info: dict[str, Any]):
        creds = Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
        self._sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    def get_metadata(self, spreadsheet_id: str) -> dict[str, Any]:
        return self._sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()

    def list_tabs(self, spreadsheet_id: str) -> dict[str, int]:
        meta = self.get_metadata(spreadsheet_id)
        return {sheet["properties"]["title"]: sheet["properties"]["sheetId"] for sheet in meta.get("sheets", [])}

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
        return int(response["replies"][0]["addSheet"]["properties"]["sheetId"])

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
                    self.batch_update(spreadsheet_id, [{"deleteSheet": {"sheetId": props["sheetId"]}}])
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
        self._sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueInputOption=value_input_option,
            body=body,
        ).execute()

    def clear_range(self, spreadsheet_id: str, range_a1: str) -> None:
        self._sheets.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id, range=range_a1, body={}
        ).execute()

    def batch_update(self, spreadsheet_id: str, requests: list[dict[str, Any]]) -> dict[str, Any]:
        if not requests:
            return {"replies": []}
        return (
            self._sheets.spreadsheets()
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
