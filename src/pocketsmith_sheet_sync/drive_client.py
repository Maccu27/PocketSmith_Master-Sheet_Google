"""Google Drive Wrapper – rekursive PDF-Suche unter Finanzen/.../Kontoauszüge/.

Nutzt das gleiche Service-Account-JSON wie sheets.py.
"""

from __future__ import annotations

import io
import logging
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterator

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

log = logging.getLogger(__name__)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
PDF_MIME = "application/pdf"
FOLDER_MIME = "application/vnd.google-apps.folder"
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    path: str          # voller Pfad relativ zum Wurzelordner
    parent_path: str   # Pfad ohne Filename
    modified_time: str
    size: int


class DriveClient:
    def __init__(self, credentials_info: dict[str, Any]):
        creds = Credentials.from_service_account_info(credentials_info, scopes=DRIVE_SCOPES)
        self._drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    def find_pdfs_under_marker(
        self,
        root_folder_id: str,
        *,
        folder_marker: str = "Kontoauszüge",
    ) -> list[DriveFile]:
        """Rekursive Suche: alle PDFs, deren Pfad einen Ordner mit `folder_marker` enthält."""
        marker_nfc = unicodedata.normalize("NFC", folder_marker)
        results: list[DriveFile] = []
        for f in self._walk(root_folder_id, ""):
            if not f.name.lower().endswith(".pdf"):
                continue
            if marker_nfc not in f.parent_path:
                continue
            results.append(f)
        return results

    def _walk(self, folder_id: str, current_path: str) -> Iterator[DriveFile]:
        """Yields files mit NFC-normalisierten Pfaden (macOS/Drive nutzt sonst NFD)."""
        page_token: str | None = None
        while True:
            response = (
                self._drive.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields="nextPageToken, files(id,name,mimeType,modifiedTime,size)",
                    pageSize=200,
                    pageToken=page_token,
                )
                .execute()
            )
            for child in response.get("files", []):
                name_nfc = unicodedata.normalize("NFC", child["name"])
                full = f"{current_path}/{name_nfc}" if current_path else name_nfc
                if child["mimeType"] == FOLDER_MIME:
                    yield from self._walk(child["id"], full)
                else:
                    yield DriveFile(
                        id=child["id"],
                        name=name_nfc,
                        path=full,
                        parent_path=current_path,
                        modified_time=child.get("modifiedTime", ""),
                        size=int(child.get("size", 0) or 0),
                    )
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def download_bytes(self, file_id: str) -> bytes:
        request = self._drive.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
        return buf.getvalue()

    def find_in_folder_by_name(
        self,
        parent_folder_id: str,
        name: str,
        *,
        mime_type: str | None = None,
    ) -> str | None:
        """Returns file_id if found, else None."""
        query = f"'{parent_folder_id}' in parents and trashed=false and name='{name}'"
        if mime_type:
            query += f" and mimeType='{mime_type}'"
        result = self._drive.files().list(q=query, fields="files(id,name)", pageSize=5).execute()
        files = result.get("files", [])
        if files:
            return files[0]["id"]
        return None

    def create_spreadsheet_in_folder(self, parent_folder_id: str, name: str) -> str:
        """Erstellt eine leere Google Sheet im Drive-Ordner und gibt die Sheet-ID zurück."""
        body = {
            "name": name,
            "mimeType": SPREADSHEET_MIME,
            "parents": [parent_folder_id],
        }
        result = self._drive.files().create(body=body, fields="id").execute()
        return result["id"]
