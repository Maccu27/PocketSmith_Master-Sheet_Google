"""PDF-Parsing-Subpackage.

Exportiert die zwei Extraktor-Typen + den generischen PDF-Client:
- KontoauszugExtractor (für Bank-Auszüge → Master Sheet)
- BelegExtractor (für Rechnungen/Quittungen → Beleg-Kontroll-Sheet, geplant)

Backward Compatibility: die Symbole, die vorher in `pdf_extractor.py`
exportiert waren (PDFExtractor, ExtractionResult, TransactionEntry,
EXTRACTION_TOOL, DEFAULT_MODEL), werden hier re-exportiert.
"""

from __future__ import annotations

from .beleg import BELEG_TOOL, BelegExtractionResult, BelegExtractor
from .client import DEFAULT_MODEL, PDFClient
from .kontoauszug import (
    EXTRACTION_TOOL,
    ExtractionResult,
    KontoauszugExtractor,
    TransactionEntry,
)

# Backward-Compat-Alias (alte Name vor dem Refactor)
PDFExtractor = KontoauszugExtractor

__all__ = [
    "DEFAULT_MODEL",
    "PDFClient",
    # Kontoauszug
    "KontoauszugExtractor",
    "PDFExtractor",  # deprecated alias
    "ExtractionResult",
    "TransactionEntry",
    "EXTRACTION_TOOL",
    # Beleg
    "BelegExtractor",
    "BelegExtractionResult",
    "BELEG_TOOL",
]
