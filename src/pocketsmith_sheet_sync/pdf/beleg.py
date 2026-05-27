"""Beleg-Extraktor.

Liest Belege (Rechnungen, Kassenzettel, Quittungen, Online-Bestätigungen,
Mahnungen, Stornos) aus PDF und extrahiert strukturierte Daten via
Claude Sonnet 4.6 mit Tool-Use.

Output ist `BelegExtractionResult`. Das eigentliche `BelegRecord` mit
File-Metadaten und Auto-Naming-Status entsteht im belege/-Submodul,
das diesen Extraktor nutzt.

Confidence-Logik:
- >= 0.9 → Auto-Naming + Auto-Sheet-Eintrag
- < 0.9 → Sheet-Eintrag mit Status „Review nötig", kein Auto-Naming

Hinweis zu Datum bei Belegen ohne Jahr (z.B. Bons mit nur „14.01."):
Der Aufrufer übergibt `fallback_year` via user_instruction. Claude wird
explizit angewiesen, das zu nutzen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from .client import DEFAULT_MODEL, PDFClient

log = logging.getLogger(__name__)


ReceiptType = Literal[
    "rechnung",
    "kassenzettel",
    "quittung",
    "bestaetigung",  # Online-Bestellbestätigung
    "mahnung",
    "storno",
    "sonstiges",
]


BELEG_TOOL = {
    "name": "extract_receipt",
    "description": (
        "Extract structured data from a receipt PDF. Receipts can be: invoices "
        "(Rechnungen, structured A4 layout), thermal receipts (Kassenzettel, "
        "narrow strips), handwritten receipts (Quittungen), online order "
        "confirmations (Bestätigungen from Amazon, eBay etc.), payment "
        "reminders (Mahnungen) or storno/refund documents. Languages: "
        "primarily German and Italian; English/French/Spanish possible. "
        "Fields you cannot determine confidently: set to null and explain "
        "in notes. Lower the confidence score accordingly."
    ),
    "input_schema": {
        "type": "object",
        "required": [
            "merchant_name",
            "transaction_date",
            "total_amount",
            "currency",
            "is_online_receipt",
            "confidence",
            "notes",
        ],
        "properties": {
            "merchant_name": {
                "type": "string",
                "description": (
                    "Name des Händlers/Dienstleisters wie auf dem Beleg. "
                    "Wenn möglich der vollständige rechtliche Name "
                    "(z. B. 'Amazon EU S.à r.l.', 'EDEKA Müller OHG'), "
                    "nicht nur das Markenkürzel. Wenn nur ein Filialname "
                    "oder Markenname erkennbar ist, diesen nehmen."
                ),
            },
            "transaction_date": {
                "type": "string",
                "description": (
                    "Datum der Transaktion (NICHT Druckdatum) im Format YYYY-MM-DD. "
                    "Bei Bons ohne Jahresangabe (z. B. nur '14.01.'): nutze den "
                    "fallback_year, den der Aufrufer im user-Text mitliefert. "
                    "Bei Online-Bestätigungen: Bestelldatum (NICHT Liefer- oder "
                    "Versanddatum)."
                ),
            },
            "transaction_time": {
                "type": ["string", "null"],
                "description": (
                    "Uhrzeit im Format HH:MM (24h), falls auf dem Beleg vorhanden. "
                    "Bei Online-Bestätigungen ohne Uhrzeit: null. "
                    "Bei Kassenzetteln: meist am Ende neben dem Datum gedruckt."
                ),
            },
            "total_amount": {
                "type": "number",
                "description": (
                    "Bruttobetrag (mit MwSt) in Originalwährung. Positiv für "
                    "Zahlungen, negativ für Rückerstattungen/Stornos. "
                    "Endbetrag, NICHT Zwischensumme."
                ),
            },
            "currency": {
                "type": "string",
                "description": "ISO-Code (EUR, USD, GBP, CHF, ...).",
            },
            "vat_amount": {
                "type": ["number", "null"],
                "description": (
                    "Ausgewiesener MwSt-Betrag in Originalwährung, falls auf "
                    "dem Beleg explizit angegeben. NICHT selbst berechnen. "
                    "Bei mehreren MwSt-Sätzen: Summe aller MwSt-Beträge. "
                    "Wenn nicht angegeben: null."
                ),
            },
            "vat_rate": {
                "type": ["number", "null"],
                "description": (
                    "Dominanter MwSt-Satz als Dezimalzahl (0.19 für 19%, 0.07 "
                    "für 7%, 0.22 für italienische 22% etc.). Bei mehreren "
                    "Sätzen: den größten Anteil nehmen. Wenn nicht erkennbar: null."
                ),
            },
            "address": {
                "type": ["string", "null"],
                "description": (
                    "Adresse des Händlers oder Filiale (Straße + PLZ + Stadt, "
                    "kompakt in einer Zeile). Bei Online-Belegen: meist null."
                ),
            },
            "items_summary": {
                "type": ["string", "null"],
                "description": (
                    "Kurze Zusammenfassung der gekauften Artikel/Leistungen "
                    "(max ~150 Zeichen). Beispiele: 'Druckerpatronen Brother', "
                    "'2x Cappuccino, 1x Croissant', 'Hotelübernachtung 14.-16.04.'. "
                    "Wenn unklar: null."
                ),
            },
            "payment_method": {
                "type": ["string", "null"],
                "description": (
                    "Zahlungsart wie auf Beleg, kompakt. Beispiele: "
                    "'Kreditkarte Visa', 'PayPal', 'Bar', 'EC-Karte', "
                    "'Lastschrift'. Wenn nicht angegeben: null."
                ),
            },
            "is_online_receipt": {
                "type": "boolean",
                "description": (
                    "true: PDF einer Online-Bestellbestätigung, Rechnung per "
                    "Email, oder per-Email-zugeschickter Beleg ohne Uhrzeit. "
                    "false: physischer Beleg (Kassenzettel, gedruckte Rechnung, "
                    "abfotografierte Quittung). Wichtig für die Chronologie-"
                    "Sortierung (online = vor physisch bei gleichem Datum)."
                ),
            },
            "receipt_type": {
                "type": ["string", "null"],
                "enum": [
                    "rechnung",
                    "kassenzettel",
                    "quittung",
                    "bestaetigung",
                    "mahnung",
                    "storno",
                    "sonstiges",
                    None,
                ],
                "description": (
                    "Beleg-Typ. 'bestaetigung' = Online-Bestellbestätigung. "
                    "'storno' wenn negativer Betrag/Rückerstattung. "
                    "'sonstiges' nur wenn nichts anderes passt."
                ),
            },
            "language": {
                "type": ["string", "null"],
                "description": "Sprache des Belegs als ISO-Code (de, it, en, fr, es, ...).",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": (
                    "Wie sicher ist die Extraktion insgesamt (0..1). "
                    ">= 0.9: alle Pflichtfelder klar erkennbar, Beleg gut lesbar. "
                    "0.7-0.89: ein Feld unsicher (z. B. Datum ohne Jahr), Rest OK. "
                    "0.5-0.69: mehrere Felder unsicher, Foto-Qualität schwach. "
                    "< 0.5: Beleg kaum lesbar, Pflichtfelder geraten."
                ),
            },
            "notes": {
                "type": "string",
                "description": (
                    "Hinweise auf Unsicherheiten, mehrdeutige Angaben, oder "
                    "Auffälligkeiten. Bei confidence >= 0.95 kann auch leer sein."
                ),
            },
        },
    },
}


@dataclass(frozen=True)
class BelegExtractionResult:
    """Was Claude aus einem Beleg-PDF extrahiert.

    Reine Inhalts-Daten — File-Metadaten (Pfad, Drive-ID, Auto-Naming-
    Status) werden im belege/-Submodul ergänzt.
    """

    merchant_name: str
    transaction_date: str  # YYYY-MM-DD
    transaction_time: str | None  # HH:MM oder None
    total_amount: float
    currency: str  # ISO
    vat_amount: float | None
    vat_rate: float | None  # 0.19, 0.07, ...
    address: str | None
    items_summary: str | None
    payment_method: str | None
    is_online_receipt: bool
    receipt_type: str | None
    language: str | None
    confidence: float
    notes: str

    @property
    def needs_review(self) -> bool:
        """True wenn Confidence unter 0.9 → manueller Review nötig."""
        return self.confidence < 0.9


class BelegExtractor:
    """Extrahiert Beleg-Daten aus PDF via Claude Sonnet 4.6.

    Nutzt den gemeinsamen PDFClient für die Anthropic-Mechanik.
    Tool-Schema und System-Prompt sind Beleg-spezifisch.
    """

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        self._client = PDFClient(api_key=api_key, model=model)

    def extract(
        self,
        pdf_bytes: bytes,
        *,
        pdf_filename: str,
        fallback_year: int | None = None,
    ) -> BelegExtractionResult:
        """Verarbeitet einen Beleg-PDF und liefert strukturiertes Ergebnis.

        Args:
            pdf_bytes: Roh-Bytes der PDF.
            pdf_filename: Dateiname (für Logging/document-title).
            fallback_year: Jahr aus dem Drive-Ordner-Pfad (z. B. 2026
                wenn Datei unter `Belege/2026/03/` liegt). Wird genutzt
                bei Bons, die nur „14.01." ohne Jahr ausweisen.

        Returns:
            BelegExtractionResult mit Confidence-Score. Aufrufer prüft
            `needs_review` und routet entsprechend.
        """
        system_prompt = (
            "Du bist ein Beleg-Extractor. Lies den vorgelegten Beleg "
            "(Rechnung, Kassenzettel, Quittung, Online-Bestellbestätigung, "
            "Mahnung oder Storno) und extrahiere die geforderten Felder "
            "exakt.\n\n"
            "Wichtige Regeln:\n"
            "1. transaction_date IMMER im Format YYYY-MM-DD. Wenn Jahresangabe "
            "auf dem Beleg fehlt, nutze den fallback_year aus dem User-Text.\n"
            "2. total_amount ist der ENDBETRAG (mit MwSt), nicht Zwischensumme. "
            "Negativ bei Rückerstattungen/Stornos.\n"
            "3. vat_amount nur ausfüllen wenn auf Beleg ausgewiesen — NICHT "
            "selbst berechnen.\n"
            "4. is_online_receipt: bei PDFs aus Email-Bestätigungen oder "
            "Web-Shops = true. Bei physischen Belegen (Foto/Scan) = false.\n"
            "5. transaction_time nur ausfüllen wenn explizit auf Beleg "
            "(meist bei Kassenzetteln am Ende). Bei Online-Belegen: null.\n"
            "6. Confidence ehrlich setzen — bei jeder Unsicherheit unter 0.9 "
            "und im notes-Feld erklären."
        )

        fallback_text = (
            f"Falls auf dem Beleg keine Jahresangabe steht, gehe von "
            f"fallback_year={fallback_year} aus (das ist der Jahres-Ordner, "
            f"in dem die Datei liegt).\n\n"
            if fallback_year is not None
            else ""
        )

        user_instruction = (
            f"Datei: {pdf_filename}\n\n"
            f"{fallback_text}"
            f"Extrahiere alle geforderten Felder mit dem extract_receipt Tool."
        )

        tool_payload = self._client.call_with_tool(
            pdf_bytes,
            pdf_filename=pdf_filename,
            tool=BELEG_TOOL,
            system_prompt=system_prompt,
            user_instruction=user_instruction,
            max_tokens=4096,  # Belege sind kleiner als Bank-Auszüge
            cache_system_prompt=True,
        )

        return BelegExtractionResult(
            merchant_name=str(tool_payload.get("merchant_name", "")).strip(),
            transaction_date=str(tool_payload.get("transaction_date", "")),
            transaction_time=_str_or_none(tool_payload.get("transaction_time")),
            total_amount=float(tool_payload.get("total_amount", 0.0) or 0.0),
            currency=str(tool_payload.get("currency", "EUR")).upper(),
            vat_amount=_float_or_none(tool_payload.get("vat_amount")),
            vat_rate=_float_or_none(tool_payload.get("vat_rate")),
            address=_str_or_none(tool_payload.get("address")),
            items_summary=_str_or_none(tool_payload.get("items_summary"), max_len=200),
            payment_method=_str_or_none(tool_payload.get("payment_method")),
            is_online_receipt=bool(tool_payload.get("is_online_receipt", False)),
            receipt_type=_str_or_none(tool_payload.get("receipt_type")),
            language=_str_or_none(tool_payload.get("language")),
            confidence=float(tool_payload.get("confidence", 0.0) or 0.0),
            notes=str(tool_payload.get("notes", "")),
        )


def _str_or_none(value: object, *, max_len: int | None = None) -> str | None:
    """Konvertiert leere Strings / None / 'null' → None. Kürzt optional."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("null", "none"):
        return None
    if max_len is not None and len(s) > max_len:
        s = s[:max_len]
    return s


def _float_or_none(value: object) -> float | None:
    """Konvertiert None / leere Werte → None, sonst float."""
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
