from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    pocketsmith_api_key: str = Field(..., description="PocketSmith X-Developer-Key")

    google_service_account_file: str | None = Field(default=None)
    google_service_account_json: str | None = Field(default=None)

    master_sheet_2026: str | None = Field(default=None)
    master_sheet_2025: str | None = Field(default=None)
    master_sheet_2024: str | None = Field(default=None)

    drive_finanzen_folder_id: str | None = Field(default=None)
    verified_label: str = Field(default="Verifiziert")
    sync_years: str = Field(default="2026")

    @field_validator("sync_years")
    @classmethod
    def _validate_years(cls, v: str) -> str:
        for piece in v.split(","):
            int(piece.strip())
        return v

    @property
    def years(self) -> list[int]:
        return [int(y.strip()) for y in self.sync_years.split(",") if y.strip()]

    @property
    def sheets_per_year(self) -> dict[int, str]:
        mapping: dict[int, str] = {}
        for year in self.years:
            attr = f"master_sheet_{year}"
            sheet_id = getattr(self, attr, None)
            if sheet_id:
                mapping[year] = sheet_id
        return mapping

    def google_credentials_info(self) -> dict[str, Any]:
        if self.google_service_account_json:
            return json.loads(self.google_service_account_json)
        if self.google_service_account_file:
            value = self.google_service_account_file.strip()
            # Toleriere häufigen Fehler: JSON-Inhalt landet versehentlich in der
            # _FILE-Variable. Wenn der Wert mit "{" anfängt, ist es ganz sicher
            # JSON-Content und kein Pfad.
            if value.startswith("{"):
                return json.loads(value)
            return json.loads(Path(value).read_text())
        raise RuntimeError(
            "No Google service account configured. Set GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_SERVICE_ACCOUNT_JSON."
        )


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
