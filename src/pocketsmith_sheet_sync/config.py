from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    pocketsmith_api_key: str = Field(..., description="PocketSmith X-Developer-Key")

    google_service_account_file: str | None = Field(default=None)
    google_service_account_json: str | None = Field(default=None)

    drive_finanzen_folder_id: str | None = Field(default=None)
    verified_label: str = Field(default="Verifiziert")
    sync_years: str = Field(default="2026")

    # PDF-Parser
    anthropic_api_key: str | None = Field(default=None)
    anthropic_model: str = Field(default="claude-sonnet-4-6")
    pdf_tracking_sheet_id: str | None = Field(default=None)
    # Komma-getrennte Liste von Ordnernamen (NFC-normalisiert) unter denen
    # PDFs gefunden werden. Beispiele: "Kontoauszüge", "Jahresauszüge",
    # "Transaktionsauszüge". Default deckt DKB + PayPal-Jahresauszüge ab.
    pdf_kontoauszug_folder_marker: str = Field(default="Kontoauszüge,Jahresauszüge")

    @property
    def folder_markers(self) -> list[str]:
        return [m.strip() for m in self.pdf_kontoauszug_folder_marker.split(",") if m.strip()]

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
        """Liest MASTER_SHEET_<YEAR> dynamisch aus os.environ + .env.

        Pydantic-Settings würde nur explizit deklarierte Felder lesen — bei
        24+ Jahren wäre das zu starr. Wir gehen direkt über die Umgebung,
        damit beliebige Jahre konfigurierbar sind.
        """
        # Lade .env zur Sicherheit nochmal (für lokale Tests; auf Railway
        # stehen die Werte direkt in os.environ).
        env_values: dict[str, str] = {}
        env_file = Path(self.model_config.get("env_file") or ".env")
        if env_file.exists():
            try:
                from dotenv import dotenv_values
                env_values = {k: v for k, v in dotenv_values(env_file).items() if v}
            except Exception:
                pass

        mapping: dict[int, str] = {}
        for year in self.years:
            key = f"MASTER_SHEET_{year}"
            sheet_id = os.environ.get(key) or env_values.get(key)
            if sheet_id:
                mapping[year] = sheet_id
        return mapping

    def get_master_sheet_id(self, year: int) -> str | None:
        return self.sheets_per_year.get(year)

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
