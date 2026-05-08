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
        "Extract structured data from a German bank statement PDF, including the "
        "complete list of individual transactions with date, amount and description. "
        "Match the statement to exactly one account from the provided account list "
        "using IBAN, account number, bank name. If no match is confident, set "
        "matched_account_id to null and explain why in notes."
    ),
    "input_schema": {
        "type": "object",
        "required": [
            "bank_name",
            "iban_or_account_number",
            "statement_period_start",
            "statement_period_end",
            "starting_balance",
            "ending_balance",
            "currency",
            "transactions",
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
                "description": "Beginn des Auszugs-Zeitraums (YYYY-MM-DD). Bei DKB typisch der 5. des Monats.",
            },
            "statement_period_end": {
                "type": "string",
                "description": "Ende des Auszugs-Zeitraums (YYYY-MM-DD). Bei DKB typisch der 4. des Folgemonats.",
            },
            "starting_balance": {
                "type": "number",
                "description": (
                    "Anfangssaldo zu Beginn des Auszugs (= Endsaldo des vorherigen Auszugs). "
                    "Negativ bei Soll. Wird für einen Konsistenz-Check zwischen aufeinanderfolgenden Auszügen genutzt."
                ),
            },
            "ending_balance": {
                "type": "number",
                "description": "Endsaldo am Stichtag des Auszugs. Negativ wenn Schulden.",
            },
            "currency": {"type": "string", "description": "Währung als ISO-Code (EUR, USD, ...)."},
            "transactions": {
                "type": "array",
                "description": (
                    "Liste ALLER Buchungen im Auszug, in der Reihenfolge wie im PDF. "
                    "Jede Soll- oder Habenposition ist EIN Eintrag. Auch Gebühren, Zinsen, Storno gehören dazu."
                ),
                "items": {
                    "type": "object",
                    "required": ["date", "amount", "description"],
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": (
                                "Buchungsdatum im Format YYYY-MM-DD. Wenn nur Wertstellung "
                                "und Buchung getrennt sind: nimm das Buchungsdatum."
                            ),
                        },
                        "amount": {
                            "type": "number",
                            "description": "Positiv für Habenbuchung, negativ für Sollbuchung.",
                        },
                        "description": {
                            "type": "string",
                            "description": "Verwendungszweck/Empfänger – kompakt, max ~100 Zeichen.",
                        },
                    },
                },
            },
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
class TransactionEntry:
    date: str   # YYYY-MM-DD
    amount: float
    description: str


@dataclass(frozen=True)
class ExtractionResult:
    bank_name: str
    iban_or_account_number: str
    statement_period_start: str
    statement_period_end: str
    starting_balance: float
    ending_balance: float
    currency: str
    transactions: list[TransactionEntry]
    matched_account_id: int | None
    confidence: float
    notes: str

    @property
    def transaction_count(self) -> int:
        return len(self.transactions)


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
            # 8192 reicht auch für volle Tx-Liste eines Bank-Statements
            # (typisch 30-100 Tx, ~50 Tokens pro Tx).
            max_tokens=8192,
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

        raw_transactions = tool_payload.get("transactions") or []
        transactions: list[TransactionEntry] = []
        for tx in raw_transactions:
            try:
                transactions.append(TransactionEntry(
                    date=str(tx.get("date", "")),
                    amount=float(tx.get("amount", 0.0) or 0.0),
                    description=str(tx.get("description", ""))[:200],
                ))
            except (TypeError, ValueError) as exc:
                log.warning("Skipping malformed tx in PDF %s: %s", pdf_filename, exc)

        return ExtractionResult(
            bank_name=str(tool_payload.get("bank_name", "")),
            iban_or_account_number=str(tool_payload.get("iban_or_account_number", "")),
            statement_period_start=str(tool_payload.get("statement_period_start", "")),
            statement_period_end=str(tool_payload.get("statement_period_end", "")),
            starting_balance=float(tool_payload.get("starting_balance", 0.0) or 0.0),
            ending_balance=float(tool_payload.get("ending_balance", 0.0) or 0.0),
            currency=str(tool_payload.get("currency", "EUR")).upper(),
            transactions=transactions,
            matched_account_id=(
                int(tool_payload["matched_account_id"])
                if tool_payload.get("matched_account_id") is not None
                else None
            ),
            confidence=float(tool_payload.get("confidence", 0.0) or 0.0),
            notes=str(tool_payload.get("notes", "")),
        )
