"""Generischer Anthropic-PDF-Client.

Gemeinsame Infrastruktur für Kontoauszug- und Beleg-Extraktion:
PDF → Base64 → Claude (mit Tool-Use, Prompt-Caching, Retries) →
Tool-Payload als dict.

Spezifische Extraktoren (KontoauszugExtractor, BelegExtractor) liefern
ihr eigenes Tool-Schema und ihren System-Prompt; das Wrapping um den
Anthropic-Call passiert hier.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import anthropic

log = logging.getLogger(__name__)

# Sonnet 4.6 ist günstig, schnell genug, und gut bei Tabellen.
DEFAULT_MODEL = "claude-sonnet-4-6"


class PDFClient:
    """Dünner Wrapper um anthropic.Anthropic für PDF + Tool-Use.

    Drei Verantwortlichkeiten:
      1. Client-Setup mit Retries (max_retries=8 → respektiert
         Anthropic's retry-after-Header).
      2. PDF → Base64 + Message-Aufbau mit document-Block.
      3. Tool-Use-Response → Tool-Payload als dict zurück.

    Spezifische Logik (Tool-Schema, System-Prompt, Datenmodell) liegt im
    jeweiligen Extraktor (kontoauszug.py, beleg.py).
    """

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        # 8 Retries mit exponential backoff (Default ist 2) — bei Rate-Limit
        # gibt Anthropic in den Headers retry_after zurück, der SDK-Client
        # respektiert das automatisch.
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=8)
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def call_with_tool(
        self,
        pdf_bytes: bytes,
        *,
        pdf_filename: str,
        tool: dict[str, Any],
        system_prompt: str,
        user_instruction: str,
        max_tokens: int = 8192,
        cache_system_prompt: bool = True,
    ) -> dict[str, Any]:
        """Schickt PDF + Tool an Claude und liefert das tool_use.input dict.

        Args:
            pdf_bytes: Roh-Bytes der PDF.
            pdf_filename: Dateiname (nur für Logging/document-title).
            tool: Tool-Schema im Anthropic-Format
                  ({"name": ..., "description": ..., "input_schema": ...}).
            system_prompt: System-Prompt-Text (bei wiederkehrendem Inhalt
                  via cache_system_prompt=True billiger).
            user_instruction: Konkrete Anweisung an Claude (folgt im
                  user-message neben der PDF).
            max_tokens: Default 8192. Reicht für ~100 Tx oder ~30 Beleg-Items.
            cache_system_prompt: True → System-Prompt wird via
                  cache_control: ephemeral gecached. Lohnt sich, wenn der
                  Prompt für viele Calls identisch ist.

        Returns:
            dict — direkt das `input`-Feld des tool_use-Blocks aus der
            Claude-Response.

        Raises:
            RuntimeError, wenn Claude keinen tool_use-Block liefert.
        """
        b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")

        system_payload: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": system_prompt,
                **({"cache_control": {"type": "ephemeral"}} if cache_system_prompt else {}),
            }
        ]

        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system_payload,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
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
                            "text": user_instruction,
                        },
                    ],
                }
            ],
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == tool["name"]:
                return block.input  # type: ignore[return-value]

        raise RuntimeError(
            f"Claude lieferte keinen tool_use-Block für Tool '{tool['name']}' "
            f"(PDF: {pdf_filename})"
        )
