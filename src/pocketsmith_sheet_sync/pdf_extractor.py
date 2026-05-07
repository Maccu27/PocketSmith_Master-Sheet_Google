"""PDF → strukturierte Bank-Auszug-Daten via Anthropic Claude.

Schickt eine PDF + die Liste bekannter PocketSmith transaction_accounts an Claude.
Claude antwortet mit Tool-Use (strukturiertes JSON) — Konto-Match, Periode,
Anzahl Transaktionen und Endsaldo.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

import anthropic

from .pocketsmith import Account

log = logging.getLogger(__name__)

# Sonnet 4.6 ist günstig, schnell genug, und gut bei Tabellen.
DEFAULT_MODEL = "claude-sonnet-4-6"

EXTRACTION_TOOL = {
    "name": "extract_bank_statement",
    "description": (
        "Extract structured data from a bank statement PDF. Match the statement to "
        "exactly one account from the provided account list using IBAN, account number, "
        "bank name, or other identifying information. If no match is confident, set "
        "matched_account_id to null and explain why in notes."
    ),
    "input_schema": {
        "type": "object",
        "required": [
            "bank_name",
            "iban_or_account_number",
            "statement_period_start",
            "statement_period_end",
            "transaction_count",
            "ending_balance",
            "currency",
            "matched_account_id",
            "confidence",
            "notes",
        ],
        "properties": {
            "bank_name": {"type": "string", "description": "Bank wie auf der PDF (z. B. 'DKB AG')."},
            "iban_or_account_number": {
                "type": "string",
                "description": "IBAN oder Kontonummer aus der PDF; leer wenn nicht vorhanden.",
            },
            "statement_period_start": {
                "type": "string",
                "description": "Beginn des Auszugs-Zeitraums im Format YYYY-MM-DD.",
            },
            "statement_period_end": {
                "type": "string",
                "description": "Ende des Auszugs-Zeitraums im Format YYYY-MM-DD.",
            },
            "transaction_count": {
                "type": "integer",
                "description": "Anzahl Buchungen (Soll- und Habenposten zusammen).",
            },
            "ending_balance": {
                "type": "number",
                "description": "Endsaldo am Stichtag des Auszugs. Negativ wenn Schulden.",
            },
            "currency": {"type": "string", "description": "Währung als ISO-Code (EUR, USD, ...)."},
            "matched_account_id": {
                "type": ["integer", "null"],
                "description": "ID aus der bereitgestellten Account-Liste oder null.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Wie sicher ist das Match auf 0..1.",
            },
            "notes": {
                "type": "string",
                "description": "Begründung des Matchings, Auffälligkeiten, oder leer.",
            },
        },
    },
}


@dataclass(frozen=True)
class ExtractionResult:
    bank_name: str
    iban_or_account_number: str
    statement_period_start: str
    statement_period_end: str
    transaction_count: int
    ending_balance: float
    currency: str
    matched_account_id: int | None
    confidence: float
    notes: str


def _account_summary(account: Account) -> dict[str, Any]:
    """Kompakte Repräsentation für den LLM-Prompt."""
    # Versuche IBAN aus Namen zu extrahieren (oft als Suffix " - DE12...")
    iban = ""
    if " - " in account.name:
        possible = account.name.rsplit(" - ", 1)[1].strip()
        # IBAN ist 22-34 Zeichen, alphanumerisch, beginnt mit 2 Buchstaben
        if 15 <= len(possible) <= 40 and possible[:2].isalpha():
            iban = possible.replace(" ", "")
    return {
        "id": account.id,
        "name": account.name,
        "bank": account.institution or "",
        "currency": account.currency,
        "iban_or_number": iban,
    }


class PDFExtractor:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        # 8 Retries mit exponential backoff (Default ist 2) — bei Rate-Limit
        # gibt Anthropic in den Headers retry_after zurück, der SDK-Client
        # respektiert das automatisch.
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=8)
        self._model = model

    def extract(
        self,
        pdf_bytes: bytes,
        *,
        accounts: list[Account],
        pdf_filename: str = "statement.pdf",
    ) -> ExtractionResult:
        """Verarbeitet eine PDF und liefert strukturiertes Ergebnis."""
        account_list = [_account_summary(a) for a in accounts]
        b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")

        # System-Prompt enthält die ~93 Konten und ist für jeden Sync-Lauf
        # identisch → mit cache_control wird er nach dem 1. Call zu 0,1×
        # Token-Kosten und zählt entsprechend gegen das Rate Limit.
        system_prompt_text = (
            "Du bist ein Bank-Statement-Extractor. Lies den deutschen Bankauszug und "
            "extrahiere die geforderten Felder genau. Zähle Buchungen (Transaktionen) — "
            "sowohl Soll als auch Haben — als einzelne Positionen. Endsaldo ist der "
            "letzte Saldo am Auszugsende, mit korrektem Vorzeichen.\n\n"
            "Beim Matching auf die Account-Liste: bevorzuge IBAN-Match, dann Kontonummer, "
            "dann Bank+Kontoart. Wenn unsicher, setze confidence < 0.8 und erkläre in notes.\n\n"
            "Account-Liste:\n"
            + "\n".join(
                f"  id={a['id']} | {a['bank']} | {a['name']} | iban/nr={a['iban_or_number'] or '<keine>'}"
                for a in account_list
            )
        )

        response = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": system_prompt_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": EXTRACTION_TOOL["name"]},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": b64,
                            },
                            "title": pdf_filename,
                        },
                        {
                            "type": "text",
                            "text": (
                                f"Datei: {pdf_filename}\n\n"
                                "Extrahiere alle geforderten Felder mit dem extract_bank_statement Tool."
                            ),
                        },
                    ],
                }
            ],
        )

        # Find tool_use block in response
        tool_payload: dict[str, Any] | None = None
        for block in response.content:
            if block.type == "tool_use" and block.name == EXTRACTION_TOOL["name"]:
                tool_payload = block.input  # type: ignore[assignment]
                break

        if tool_payload is None:
            raise RuntimeError("Claude lieferte keinen tool_use-Block")

        return ExtractionResult(
            bank_name=str(tool_payload.get("bank_name", "")),
            iban_or_account_number=str(tool_payload.get("iban_or_account_number", "")),
            statement_period_start=str(tool_payload.get("statement_period_start", "")),
            statement_period_end=str(tool_payload.get("statement_period_end", "")),
            transaction_count=int(tool_payload.get("transaction_count", 0) or 0),
            ending_balance=float(tool_payload.get("ending_balance", 0.0) or 0.0),
            currency=str(tool_payload.get("currency", "EUR")).upper(),
            matched_account_id=(
                int(tool_payload["matched_account_id"])
                if tool_payload.get("matched_account_id") is not None
                else None
            ),
            confidence=float(tool_payload.get("confidence", 0.0) or 0.0),
            notes=str(tool_payload.get("notes", "")),
        )
