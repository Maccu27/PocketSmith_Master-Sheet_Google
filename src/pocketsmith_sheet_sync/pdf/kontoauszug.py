"""Kontoauszug-Extraktor.

Vorher: pdf_extractor.py (Top-Level-Modul).
Jetzt: pdf/kontoauszug.py, nutzt PDFClient als Common-Layer.

Funktional unverändert. Nur die Anthropic-Mechanik (Client-Setup,
Base64, Message-Aufbau, Tool-Use-Parsing) liegt jetzt in PDFClient.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..pocketsmith import Account
from .client import DEFAULT_MODEL, PDFClient

log = logging.getLogger(__name__)


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
                        "tx_type": {
                            "type": "string",
                            "description": (
                                "Bei PayPal-Auszügen: der technische Buchungstyp aus der "
                                "'Description'-Spalte VOR dem Doppelpunkt. Beispiele: "
                                "'Express Checkout Payment', 'PreApproved Payment Bill User Payment', "
                                "'General Card Deposit', 'PayPal Buyer Credit Payment Funding', "
                                "'Bank Deposit to PP Account', 'Mobile Payment', "
                                "'User Initiated Withdrawal', 'General Card Withdrawal', "
                                "'Payment Refund', 'General Buyer Credit Payment', "
                                "'General Currency Conversion', 'Void of Authorization', "
                                "'General Authorization', 'General Payment'. "
                                "Bei normalen Bankauszügen leer lassen."
                            ),
                        },
                        "status": {
                            "type": "string",
                            "description": (
                                "Bei PayPal-Auszügen: Status aus der 'Status'-Spalte "
                                "('Completed', 'Pending', 'Reversed', 'Denied'). Bei normalen "
                                "Bankauszügen leer lassen."
                            ),
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
    date: str  # YYYY-MM-DD
    amount: float
    description: str
    tx_type: str = ""  # nur bei PayPal-Auszügen gefüllt
    status: str = ""  # nur bei PayPal-Auszügen gefüllt


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


class KontoauszugExtractor:
    """Extrahiert Bank-Auszüge aus PDF und matched sie auf PocketSmith-Konten.

    Vorher hieß diese Klasse PDFExtractor. Der alte Name ist als Alias
    im Subpackage-`__init__.py` weiterhin verfügbar.
    """

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        self._client = PDFClient(api_key=api_key, model=model)

    def extract(
        self,
        pdf_bytes: bytes,
        *,
        accounts: list[Account],
        pdf_filename: str = "statement.pdf",
    ) -> ExtractionResult:
        """Verarbeitet eine PDF und liefert strukturiertes Ergebnis."""
        account_list = [_account_summary(a) for a in accounts]

        # System-Prompt enthält die ~93 Konten und ist für jeden Sync-Lauf
        # identisch → mit cache_control wird er nach dem 1. Call zu 0,1×
        # Token-Kosten und zählt entsprechend gegen das Rate Limit.
        system_prompt = (
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

        tool_payload = self._client.call_with_tool(
            pdf_bytes,
            pdf_filename=pdf_filename,
            tool=EXTRACTION_TOOL,
            system_prompt=system_prompt,
            user_instruction=(
                f"Datei: {pdf_filename}\n\n"
                "Extrahiere alle geforderten Felder mit dem extract_bank_statement Tool."
            ),
            max_tokens=8192,
            cache_system_prompt=True,
        )

        raw_transactions = tool_payload.get("transactions") or []
        transactions: list[TransactionEntry] = []
        for tx in raw_transactions:
            try:
                transactions.append(
                    TransactionEntry(
                        date=str(tx.get("date", "")),
                        amount=float(tx.get("amount", 0.0) or 0.0),
                        description=str(tx.get("description", ""))[:200],
                        tx_type=str(tx.get("tx_type", ""))[:80],
                        status=str(tx.get("status", ""))[:30],
                    )
                )
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
