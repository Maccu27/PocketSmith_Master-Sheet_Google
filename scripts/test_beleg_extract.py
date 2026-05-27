"""Smoke-Test für den neuen BelegExtractor.

Lädt eine Handvoll Belege aus dem Drive-Ordner `Belege und Rechnungen/2026/05`,
schickt jede durch Claude und druckt das Ergebnis als formatiertes JSON.

Ziel: zeigen, ob das Tool-Schema realitätsnah ist, bevor wir das ganze
belege/-Submodul drumherum bauen.

Aufruf:
    cd ~/projects/pocketsmith-sheet-sync
    PYTHONPATH=src .venv/bin/python3 scripts/test_beleg_extract.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

# .env aus dem Project-Root laden (Skript läuft von beliebigem cwd)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=True)

from pocketsmith_sheet_sync.config import Settings
from pocketsmith_sheet_sync.drive_client import DriveClient
from pocketsmith_sheet_sync.pdf import BelegExtractor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# Belege aus Drive → Belege und Rechnungen → 2026 → 05 Mai
# Mix: klassische Rechnung, Mini-Beleg, Online-Bestätigung, Scan
TEST_BELEGE = [
    ("Rechnung R6B311661.pdf", "1lBd9DFQN9MeHRC2OSXohfRwPPqPxB1Gt"),
    ("DE_00009_20260525_..._MOT_175.pdf", "1h1zU3up1LTB8Cvtr86X9oeOtmoD1rfkh"),
    ("CDGGPGDYK-1.pdf", "11cIAsC7UepAh-CcdwjaK6e1ElP_n_KT-"),
    ("Invoice-9BF0758D-2342742.pdf", "1LbpaZeraWAa15AjnRApmTZ6oFU4NqLxY"),
    ("20260523094837_001.pdf (Scan)", "1bj2mPz5M7ePjpWg9G6zOlsAqukbcuYv_"),
]

FALLBACK_YEAR = 2026  # aus dem Drive-Pfad: Belege/2026/05


def main() -> int:
    settings = Settings()  # liest .env
    if not settings.anthropic_api_key:
        log.error("ANTHROPIC_API_KEY fehlt in .env")
        return 1

    try:
        credentials_info = settings.google_credentials_info()
    except Exception as exc:
        log.error("Google Service Account konnte nicht geladen werden: %s", exc)
        return 1
    if not credentials_info:
        log.error("Google Service Account nicht konfiguriert")
        return 1

    drive = DriveClient(credentials_info=credentials_info)
    extractor = BelegExtractor(
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model,
    )

    results: list[dict] = []
    for label, file_id in TEST_BELEGE:
        log.info("=" * 60)
        log.info("Lade %s ...", label)
        t0 = time.perf_counter()
        try:
            pdf_bytes = drive.download_bytes(file_id)
        except Exception as exc:
            log.error("Download fehlgeschlagen: %s", exc)
            results.append({"file": label, "error": f"download: {exc}"})
            continue
        log.info("Download OK (%d bytes), schicke an Claude ...", len(pdf_bytes))

        try:
            result = extractor.extract(
                pdf_bytes,
                pdf_filename=label,
                fallback_year=FALLBACK_YEAR,
            )
        except Exception as exc:
            log.exception("Extraktion fehlgeschlagen: %s", exc)
            results.append({"file": label, "error": f"extract: {exc}"})
            continue

        elapsed = time.perf_counter() - t0
        log.info("Fertig in %.1fs — Confidence %.2f", elapsed, result.confidence)

        results.append({
            "file": label,
            "elapsed_sec": round(elapsed, 1),
            "needs_review": result.needs_review,
            **asdict(result),
        })

    print()
    print("=" * 60)
    print("ERGEBNISSE")
    print("=" * 60)
    print(json.dumps(results, indent=2, ensure_ascii=False))

    # Mini-Zusammenfassung
    print()
    print("=" * 60)
    print("KURZ-ZUSAMMENFASSUNG")
    print("=" * 60)
    for r in results:
        if "error" in r:
            print(f"  ✗ {r['file']}: {r['error']}")
            continue
        marker = "⚠ Review" if r["needs_review"] else "✓ Auto"
        print(
            f"  {marker} {r['file']}: "
            f"{r['merchant_name'][:30]} · {r['transaction_date']} · "
            f"{r['total_amount']} {r['currency']} · "
            f"conf {r['confidence']:.2f}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
