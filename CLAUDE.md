# pocketsmith-sheet-sync — Claude Code Context

This file is the entry point for any Claude Code session that picks up work
on this project. Read it first; it summarizes architecture, state and
preferences that the user expects to be respected.

## Was das Projekt macht

Marco hat ~30.000 Transaktionen in PocketSmith über 93 transaction_accounts
und kämpft mit der Vollständigkeit beim Buchen aus Kontoauszügen. Dieses Tool
syncronisiert PocketSmith-Daten in Google Sheets und vergleicht sie mit
Soll-Werten aus PDF-Kontoauszügen.

Output: pro Jahr eine Master-Sheet in Drive mit
- Übersicht (Jahres- und Monats-Aggregate)
- Konten (alle Konten mit Tx in dem Jahr, Aktiv-Häkchen)
- 12 Monatstabs (pro Konto: Anzahl/Saldo PocketSmith vs Auszug, Differenz,
  Verifiziert-Status, Gebucht-Häkchen, Notizen)
- Anleitung-Tab

Daily Cron auf Railway um 03:00 UTC füllt das alles automatisch.

## Architektur

```
GitHub (Maccu27/PocketSmith_Master-Sheet_Google)
    │
    └─→ Railway (NIXPACKS, requirements.txt, PYTHONPATH=/app/src)
        Cron 0 3 * * * → python -m pocketsmith_sheet_sync.main daily
            │
            ├─ Phase 1: PocketSmith → Sheets
            │  • PocketSmith API: list_accounts (transaction_accounts!), iter_transactions
            │  • Per Sync-Year: 14 Tabs anlegen, Layout, Konten-Filter (nur Tx>0)
            │
            ├─ Phase 2: PDF Parser
            │  • Drive crawl unter Finanzen/.../Kontoauszüge/ (rekursiv, NFC-norm.)
            │  • Anthropic Claude (Sonnet 4.6) extrahiert IBAN + Tx-Liste + Saldi
            │  • PDFTracker speichert Records im "PocketSmith PDF Tracking"-Sheet
            │  • Vier-Augen-Check: starting_balance(N) ?= ending_balance(N-1)
            │
            └─ Phase 3: Backfill
               • Aggregator: pro (account, year, month) aus Tracker-Tx-Listen
                 die echten Soll-Werte für den Kalendermonat berechnen
               • In Master-Sheets schreiben (Soll-Anzahl Spalte C, Soll-Saldo Spalte F)
```

## Module

| Datei | Zweck |
|---|---|
| `config.py` | Pydantic-Settings + dynamisches MASTER_SHEET_<YEAR> aus os.environ |
| `pocketsmith.py` | API-Client (Account, Transaction Datamodel, iter_transactions) |
| `sheets.py` | Sheets-Client + Drive-Client (gemeinsam), get_metadata-Cache |
| `aggregator.py` | PocketSmith-Aggregation: pro (account, month) count/saldo/verified |
| `formatting.py` | Format-Request-Builder (Header, Farben, Schutz, Conditional Format) |
| `sync.py` | Master-Sheet-Schreiblogik (Übersicht, Konten, Monate, Anleitung) |
| `drive_client.py` | Drive-API: rekursive PDF-Suche, Download, Sheet-Erstellung |
| `pdf_extractor.py` | Anthropic-Wrapper, Tool-Use für strukturierte Extraktion |
| `pdf_tracker.py` | "PocketSmith PDF Tracking"-Sheet als Datenbank für PDF-Records |
| `pdf_sync.py` | PDF-Pipeline + Aggregator (Kalendermonat aus Tx-Listen) + Backfill |
| `main.py` | CLI: sync, parse-pdfs, backfill, daily, reset-tracker |
| `mcp_server.py` | Lokaler stdio-MCP-Server für manuellen Trigger aus Claude Code |

## Wichtige Konzepte

### Splits
PocketSmith-Splits werden über `(date, original_payee, note, transaction_account_id)`
gruppiert. Konfigurierbar via `_group_key` in `aggregator.py`. Bewusst akzeptierter
False-Positive-Risiko bei generischen `note`-Werten ("Type: PAYPAL").

### Verifiziert vs. Beleg
Beide Konzepte arbeiten mit Splits-Gruppen, aber unterschiedlich:
- **Verifiziert**: STRIKT — alle Tx der Gruppe müssen das Label haben
- **Beleg** (geplant): LAX — mindestens 1 Tx der Gruppe hat ein Attachment

### Kalendermonat-Logik (wichtig)
DKB und andere Banken haben Auszüge **nicht** monatsgetreu (z.B. 5.5.-4.6.).
Die Sheet zeigt aber **Kalendermonat-Wahrheit**. Der Aggregator löst das so:
- Anzahl: alle Tx aus allen Tracker-Records mit `tx.date in [first, last]` des Monats
- Saldo: aus dem Auszug der den Monatsletzten enthält:
  `ending_balance - sum(tx.amount for tx in record.transactions if tx.date > last)`

### Service Account / Storage Quota
Service Accounts können auf privaten Drives **keine Files erstellen** (Quota=0).
Daher hat Marco die Tracking-Sheet manuell angelegt. Code hat einen Fallback
über `find_in_folder_by_name` + `create_spreadsheet_in_folder`, der aber nur
bei Workspace-Drives funktioniert.

### Sheets API Rate Limit
60 Reads/Min pro User. Mit 24 Master-Sheets × 14 Tabs schnell überschritten.
Caching-Layer in `SheetsClient.get_metadata()` und in `pdf_sync._row_cache` lösen das.

### Anthropic Rate Limit
Tier 1: 30k input tokens/min. Mit 64 PDFs × ~10k Tokens schnell erreicht.
Lösung: Prompt Caching auf System-Prompt (Konten-Liste) + 2s Sleep zwischen
Calls + max_retries=8 in PDFExtractor.

### Unicode NFC vs NFD
macOS und Drive geben Pfade in NFD-Normalisierung zurück (`u`+combining-diaeresis).
String-Matches gegen NFC ("Kontoauszüge") müssen normalisiert werden. In
`drive_client._walk` schon erledigt.

## Aktueller Stand (zum Zeitpunkt des CLAUDE.md-Erstellens)

- ✅ Phase 1+2+3 alle gebaut und produktiv
- ✅ Service Account, Tracking-Sheet, Master-Sheets 2003-2026 vorhanden
- ✅ Parser komplett refaktoriert auf Tx-Level + Vier-Augen-Check
- ⏳ **Tracker muss reset werden** — alte 64 Records haben altes Schema
- ⏳ **Reprocessing aller 64 PDFs nötig** — geschätzt ~15-20 € einmalig
- ⏳ **Beleg-Feature** noch offen: PocketSmith-Attachment-Status pro Tx

## Häufige Tasks

### Reset + Reprocess
```bash
# Lokal
PYTHONPATH=src python -m pocketsmith_sheet_sync.main reset-tracker --confirm
PYTHONPATH=src python -m pocketsmith_sheet_sync.main parse-pdfs

# Auf Railway: startCommand temporär ändern oder via MCP-Tool
# reset_pdf_tracker() aus Claude Code aufrufen
```

### Neues Jahr aktivieren
1. Sheet in Drive erstellen, Service-Account-Email als Editor hinzufügen
2. Railway-Variable `MASTER_SHEET_<JAHR>=<id>` setzen
3. `SYNC_YEARS` erweitern
4. Trigger Run

### Container-Timeout vermeiden
Railway-Cron hat Timeout (~30-60 Min). Bei vielen Jahren auf einmal:
- `SYNC_YEARS` reduzieren auf max. 5-6 Jahre pro Run
- Mehrere manuelle Trigger-Runs mit Subset-Listen

## Konfiguration

| Env Var | Pflicht? | Bedeutung |
|---|---|---|
| `POCKETSMITH_API_KEY` | ja | X-Developer-Key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` (Railway) oder `_FILE` (lokal) | ja | Service-Account-Auth |
| `MASTER_SHEET_<YEAR>` | mind. 1 | Pro Jahr eine Sheet-ID |
| `SYNC_YEARS` | ja | Komma-Liste, z.B. "2024,2025,2026" |
| `VERIFIED_LABEL` | nein, default "Verifiziert" | PocketSmith-Label-Name |
| `DRIVE_FINANZEN_FOLDER_ID` | für PDF-Parser | Wurzelordner |
| `ANTHROPIC_API_KEY` | für PDF-Parser | Claude API |
| `ANTHROPIC_MODEL` | nein, default "claude-sonnet-4-6" | |
| `PDF_TRACKING_SHEET_ID` | empfohlen | Manuell erstellt (Service Acc kann nicht in privatem Drive) |
| `PDF_KONTOAUSZUG_FOLDER_MARKER` | nein, default "Kontoauszüge" | Pfad-Filter |

## Marco's Präferenzen (verbindlich)

- **Sprache**: Deutsch im Chat, außer er schreibt Englisch
- **Stil**: direkt, anti-sycophancy. Falsche Prämissen sofort benennen, bessere
  Ansätze vorschlagen. Ja/Nein zuerst, dann erklären. Kein Padding.
- **Keine Abkürzungen** in Sheet-Spaltenüberschriften ("Anzahl PocketSmith",
  nicht "Anz. PS")
- **Layout**: dezent klassisch — keine schreienden Farben. Pastell-Akzente
  (hellgelb manuell, hellgrau auto, hellgrün ok, hellorange warning)
- **Sicherheit**: vor destruktiven Aktionen (rm -rf, DROP, Datei-Überschreibung,
  Deployment-Trigger) IMMER explizit fragen
- **Secrets**: niemals in Chat reproduzieren, niemals in Git committen,
  `.env`-Datei vorsichtig (Claude Code Auto-Read kann Inhalt im Chat anzeigen)

## Test-Setup lokal

```bash
cd ~/Projects/pocketsmith-sheet-sync
uv venv .venv --python 3.11
uv pip install -e ".[mcp]" --python .venv/bin/python
# .env aus .env.example erstellen, Werte eintragen
# unset ANTHROPIC_API_KEY  # Claude Code setzt das leer, blockiert .env-Wert
.venv/bin/python -m pocketsmith_sheet_sync.main sync
```

## Bekannte Stolperstellen

1. **Pydantic-Settings + Claude Code**: ANTHROPIC_API_KEY wird leer in der
   Umgebung gesetzt → unset vor lokalen Tests
2. **Tab-Namen mit Leerzeichen**: in Range-Strings immer in single quotes
   einbetten: `f"'{TAB}'!A1:Z"` statt `f"{TAB}!A1:Z"`
3. **conditionalFormatRules**: deutsche Locale → Semikolon als Argument-Trenner
   (`AND(A;B)` nicht `AND(A,B)`)
4. **Service Account Storage Quota**: bei privaten Drives kann der Service
   Account keine neuen Files anlegen — Tracking-Sheet manuell pre-erstellen
5. **Anthropic Custom Endpoint**: ANTHROPIC_BASE_URL in Claude-Code-Env kann
   API-Calls auf einen Proxy umleiten — bei lokalem Test unset

## Workflow-Regel

Code-Änderungen IMMER:
1. Kurz im Chat zusammenfassen, was sich ändert
2. Bei >3 Dateien: vorher Marco's Bestätigung
3. Nach Push warten auf Marco's Bestätigung des Railway-Deployments
4. Logs analysieren bevor nächster Schritt
