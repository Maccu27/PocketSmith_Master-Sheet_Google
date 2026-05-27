# Belege-Kontrolle — Spezifikation

**Status:** Draft v1 · **Autor:** Claude + Marco · **Datum:** 2026-05-24
**Ziel:** Erweiterung des `pocketsmith-sheet-sync`-Projekts um ein
Belege-/Rechnungs-Modul, das Lücken erkennt, Auto-Naming vergibt und
PDF-Fusions-Vorschläge macht.

---

## 1. Ziel

Aktuell sammelt Marco Belege manuell in `Drive → Belege und Rechnungen →
JJJJ → MM`. Die zentrale Frage bei jeder PocketSmith-Buchung lautet:
**„Habe ich für diese Transaktion einen Beleg?"** Heute wird das pro
Buchung einzeln in der UI geprüft — fehleranfällig und zeitintensiv.

Das neue Modul erzeugt pro Jahr eine **Belege-Kontroll-Sheet** im Drive
mit pro Monat einem Tab. Jede Bank-Buchung steht dort als Zeile, mit:
- ihrer chronologisch korrekten Belegnummer (real, wenn Beleg da; fiktiv
  wenn Lücke)
- dem Match-Status (Beleg gefunden? PocketSmith-Buchung gefunden?
  Lücke?)
- dem Auto-Naming-Status (PDF schon umbenannt? oder rohe Datei?)
- Vorschlägen für PDF-Fusionen (mehrere Dateien zur gleichen
  Transaktion)

Sekundär: das Modul benennt neue PDFs nach Schema `NNNN-YYYY` um,
basierend auf der berechneten chronologischen Position.

---

## 2. Architektur

### Einordnung in `pocketsmith-sheet-sync`

```
src/pocketsmith_sheet_sync/
├── pdf/                       ← NEU: gemeinsame PDF-Infrastruktur
│   ├── __init__.py
│   ├── client.py              ← Anthropic-Client, Base64, Cache, Throttle,
│   │                            Retries (aus pdf_extractor.py extrahiert)
│   ├── kontoauszug.py         ← UMBENANNT von pdf_extractor.py
│   └── beleg.py               ← NEU: Beleg-Extraktor
├── belege/                    ← NEU: Belege-spezifische Logik
│   ├── __init__.py
│   ├── chronologie.py         ← Sortier-Logik
│   ├── matcher.py             ← Beleg ↔ PocketSmith-Match
│   ├── merger.py              ← PDF-Fusion via pypdf
│   ├── auto_namer.py          ← NNNN-YYYY-Vergabe + Umbenennen
│   └── sheet.py               ← Beleg-Kontroll-Sheet-Generator
├── drive_client.py            ← (bleibt, evtl. um Belege-Helper erweitert)
├── pocketsmith.py             ← (bleibt)
├── sheets.py                  ← (bleibt)
└── main.py                    ← CLI um belege-sync-Command erweitert
```

### Backward Compatibility

Bestehender `pdf_extractor.py` wird **umbenannt** zu
`pdf/kontoauszug.py`. Imports im restlichen Code (`pdf_sync.py`,
`mcp_server.py`) werden entsprechend angepasst. Keine Funktionalität
geht verloren.

---

## 3. Datenmodelle

### 3.1 `BelegRecord` (Output Beleg-Extraktor)

```python
@dataclass
class BelegRecord:
    file_path: str                       # Pfad im Drive
    drive_file_id: str                   # Drive-API-ID
    
    # Aus Claude-Extraktion:
    merchant_name: str                   # "Amazon EU S.à r.l."
    transaction_date: str                # "2026-01-15"
    transaction_time: str | None         # "14:32" (None bei online)
    total_amount: float                  # 47.99
    currency: str                        # "EUR"
    vat_amount: float | None             # 7.66
    address: str | None                  # "Köln, Deutschland"
    items_summary: str | None            # "Druckerpatronen Brother"
    is_online_receipt: bool              # True wenn online (keine Uhrzeit)
    confidence: float                    # 0.0–1.0
    notes: str | None                    # Claude-Hinweise
    
    # Aus Auto-Naming:
    assigned_number: str | None          # "0042-2026" (nach Vergabe)
    is_renamed: bool                     # True wenn Datei schon NNNN-YYYY heißt
```

### 3.2 `BankBuchung` (gruppiert aus PocketSmith)

Wiederverwendung der `_group_key`-Logik aus `aggregator.py`:
`(date, original_payee, transaction_account_id)` ergibt eine
Bank-Buchung — alle Splits dieser Gruppe gehören dazu.

```python
@dataclass
class BankBuchung:
    transaction_account_id: int
    date: str                            # "2026-01-15"
    original_payee: str                  # Bank-Memo, vor Marcos Anpassung
    payee: str                           # Marcos angepasster Payee
    brutto_betrag: float                 # Summe aller Splits = Originalbetrag
    pocketsmith_transaction_ids: list[int]  # alle Split-IDs
    has_attachment: bool                 # mindestens 1 Split hat Anhang
```

### 3.3 `MatchResult` (Beleg ↔ Bank-Buchung)

```python
@dataclass
class MatchResult:
    beleg: BelegRecord | None
    buchung: BankBuchung | None
    match_status: Literal[
        "matched",                       # beide vorhanden, gematcht
        "beleg_ohne_buchung",            # Beleg da, keine PS-Tx gefunden
        "buchung_ohne_beleg",            # PS-Tx da, kein Beleg = LÜCKE
        "doppelter_beleg_kandidat",      # mehrere Belege auf 1 Buchung
    ]
    match_confidence: float              # 0.0–1.0
    match_reason: str                    # "exact date + amount + payee fuzzy"
```

---

## 4. Pipeline

### Schritt 1 — Belege-Inventar laden

- Drive-API: rekursiv alle PDFs unter
  `Belege und Rechnungen/JJJJ/MM`
- Pro PDF: ist es schon nach `NNNN-YYYY` benannt? → bestehende Nummer
  übernehmen. Sonst: später Auto-Naming.

### Schritt 2 — Belege extrahieren

- Für jede noch nicht analysierte PDF: `BelegExtraktor.extract(pdf)`
- Output: `BelegRecord` (alle Felder gefüllt)
- Cache: Records werden persistent in einem
  `Belege-Tracking-Sheet` gespeichert (analog `PDF Tracking` für
  Kontoauszüge), damit der nächste Lauf nur neue PDFs neu analysiert.

### Schritt 3 — Chronologie berechnen

Pro Monat sortiere ich `BelegRecord` nach:
1. `transaction_date` (aufsteigend)
2. Bei gleichem Datum: `is_online_receipt` zuerst (online = vor
   physisch)
3. Bei gleichem Datum + beide physisch: `transaction_time` aufsteigend
4. Bei Tie: alphabetisch nach `merchant_name`

### Schritt 4 — PocketSmith-Buchungen laden + Matching

- Hole alle `BankBuchung`-Objekte für den Monat
  (aus PocketSmith API + `aggregator.py`-Logik)
- Pro `BelegRecord`: finde beste `BankBuchung` über:
  - **Datum:** exakt, oder ±3 Tage bei Online-Belegen (Versand-Delay)
  - **Betrag:** ±0,01 € Toleranz
  - **Payee:** Fuzzy-Match (Token-basiert, `merchant_name` vs.
    `original_payee` + `payee`)
- Erzeuge `MatchResult` pro Kombination.

### Schritt 5 — Lücken erkennen + fiktive Nummern

- Alle `BankBuchung` ohne `BelegRecord` → `match_status =
  "buchung_ohne_beleg"`
- Diese werden in die chronologische Sortierung einsortiert
  (am `transaction_date` der Bank-Buchung)
- Fiktive Belegnummer: nimm die nächste reale Nummer als Anker und
  hänge `a`, `b`, `c` an
  Beispiel: zwischen `0017-2026` und `0018-2026` liegen 2 Lücken
  → `0017a-2026`, `0017b-2026`

### Schritt 6 — Auto-Naming + Umbenennen

- Belege, die noch nicht `NNNN-YYYY` heißen, bekommen die berechnete
  Nummer
- Datei wird via Drive-API umbenannt
- Logging-Eintrag: alter Name → neuer Name (für Audit-Trail)

### Schritt 7 — PDF-Fusion-Detection

- Pro `BankBuchung` mit > 1 Beleg-Match: prüfe ob die Belege zusammen
  gehören
  - Selber `merchant_name` + selbes Datum?
  - Summe der `total_amount` = `BankBuchung.brutto_betrag`?
- Wenn ja: erzeuge `MergeVorschlag` (wird im Sheet markiert,
  nicht automatisch ausgeführt). Auf manuelle Bestätigung wartet
  separater CLI-Command `belege-merge <vorschlag-id>`.

### Schritt 8 — Sheet schreiben

- Pro Jahr ein Google Sheet `Belege-Kontrolle JJJJ` in
  `Drive → Master Sheets` (oder neuer Ordner `Belege-Kontrolle`)
- Pro Monat ein Tab (`01`, `02`, ...)
- Layout siehe nächster Abschnitt

---

## 5. Sheet-Layout

### Monats-Tab

| Spalte | Beispiel |
|---|---|
| A · Chrono-Rang | `1`, `2`, `3`, … |
| B · Beleg-Nr | `0001-2026` oder `0017a-2026` (Lücke) |
| C · Datum (Beleg) | `15.01.2026` |
| D · Uhrzeit | `14:32` oder `online` |
| E · Merchant | `Amazon EU S.à r.l.` |
| F · Betrag brutto | `47,99 €` |
| G · Match-Status | `OK` / `Lücke: Beleg fehlt` / `Lücke: PS-Tx fehlt` / `Doppelt-Kandidat` |
| H · PocketSmith-Tx | Link auf die Bank-Buchung in PS (falls gematcht) |
| I · Drive-Pfad | Link auf die PDF im Drive |
| J · PDF-Renamed? | `✓` / `(noch nicht)` |
| K · Merge-Vorschlag | Leer oder `vermutlich mit 0018-2026 zusammen` |
| L · Notizen | freies Feld für manuelle Notizen |

### Übersichts-Tab

- Pro Monat: Anzahl Buchungen, Anzahl Belege, Anzahl Lücken, %-Beleg-Quote
- Top 10 Lücken nach Betrag (Priorität für Beleg-Nachreichung)

---

## 6. CLI / MCP

### Neue CLI-Commands in `main.py`

```bash
# Vollständiger Sync für ein Jahr
pocketsmith-sync belege-sync 2026

# Nur ein Monat
pocketsmith-sync belege-sync 2026 --month 1

# Nur neue PDFs analysieren (skip Match + Sheet)
pocketsmith-sync belege-extract 2026 --month 1

# Merge-Vorschlag ausführen (interaktiv)
pocketsmith-sync belege-merge <vorschlag-id>
```

### MCP-Tool-Erweiterung

`mcp_server.py` bekommt ein neues Tool `belege_sync(year, month?)` —
damit kann Claude in einer Buchungs-Sitzung den Belege-Sync triggern.

---

## 7. Trigger / Cron

**MVP (sofort):**
- Daily Cron um 03:00 UTC ruft `belege-sync` für den aktuellen Monat auf
  (analog zum bestehenden `daily`-Command für Kontoauszüge)
- Polling-basiert (15-Min-Verzug ist tolerierbar)

**Phase 2 (später):**
- Drive Push Notifications (`drive.changes.watch`) → Webhook auf
  Railway → triggert `belege-sync` für den betroffenen Monat
- Echtzeit, aber Token-Refresh alle 7 Tage notwendig

---

## 8. Edge Cases

| Case | Verhalten |
|---|---|
| Belege ohne Datum (z.B. Foto-Beleg, OCR scheitert) | `confidence < 0.5` → in Sheet als „Manuelles Review" markiert, keine Auto-Nummer |
| Beleg gehört zu mehreren Bank-Buchungen (z.B. Sammelrechnung) | `match_status = "doppelter_beleg_kandidat"`, manuelle Entscheidung |
| Bank-Buchung hat mehrere Belege (Marcos PDF-Fusion-Fall) | `merge_vorschlag` im Sheet, manuelle Bestätigung |
| Beleg-Datum vor Bank-Datum (z.B. Bestellung Dezember, Abbuchung Januar) | Match mit Toleranz ±7 Tage, mit niedrigerer Confidence |
| Dauer-Belege (Handyverträge, Versicherungen) liegen nicht im Sammelbecken | NICHT vom Modul erfasst — manueller Workflow bleibt für diese |
| Mehrere PDFs nach `NNNN-YYYY` für gleichen Tag (Lücke nachträglich) | Re-Nummerierung des betroffenen Tages, Logging |
| Fremdwährungs-Belege | Tool-Schema unterstützt `currency`; Match prüft Betrag in Originalwährung wenn Bank das so liefert |
| Bargeldkonten | NICHT vom Modul erfasst (laut SOP ausgeschlossen) |

---

## 9. Was später paperclip / Multi-Agent macht

Das hier ist **Stufe 1**: deterministisches Modul mit klaren Regeln.
**Stufe 2** (in 1-2 Monaten):
- Ein „CFO-Agent" auf paperclip.ing prüft die Sheet-Ergebnisse stichprobenartig
- Spezialisierte Sub-Agents:
  - Belege-Agent: tut was dieses Modul tut
  - Payee-Recherche-Agent: holt rechtliche Namen aus Web (falls auf
    Beleg nicht eindeutig)
  - Kategorien-Vorschlag-Agent: schlägt PocketSmith-Kategorie pro
    Buchung vor

Das Belege-Modul (Stufe 1) ist die Grundlage. Stufe 2 nutzt es als
Tool, fügt aber LLM-basierte Urteils-Schritte hinzu.

---

## 10. Entscheidungen (Marco, 2026-05-24)

1. **Output-Sheet-Ort:** Pro Jahr ein Google Sheet direkt im
   Jahres-Ordner: `Drive → Belege und Rechnungen → JJJJ →
   Belege-Kontrolle JJJJ.gsheet`
2. **Bestehende PDFs umbenennen:** Default ab heute (Cron läuft auf
   aktuellen Monat). Manueller CLI-Trigger für historische Monate:
   `belege-sync 2024 --month 3` → benennt Belege für März 2024
   nachträglich.
3. **Tracker-Sheet:** Separates internes `Belege Tracking`-Sheet
   zentral (analog `PDF Tracking` für Kontoauszüge). Marco öffnet
   das normalerweise nicht — reine Cache-Datenbank, damit der Sync
   nicht jedes Mal alle PDFs neu durch Claude jagt.
4. **Confidence-Threshold:** `>= 0.9` → Auto-Rename. Alles darunter
   → in Sheet als „Review nötig" markiert, keine automatische
   Umbenennung. Marco entscheidet manuell.
5. **PDF-Merge:** Immer manuelle Bestätigung. CLI-Command
   `belege-merge <vorschlag-id>` führt den Merge interaktiv aus.
   Kein Auto-Merge.

---

## 11. Aufwandsschätzung

| Phase | Module | Code-Aufwand |
|---|---|---|
| Phase 1 | `pdf/client.py` (Refactor aus pdf_extractor.py) | ~100 Zeilen |
| Phase 1 | `pdf/beleg.py` (neuer Extraktor) | ~150 Zeilen |
| Phase 1 | `belege/chronologie.py` + `matcher.py` | ~200 Zeilen |
| Phase 1 | `belege/sheet.py` (Sheet-Generator) | ~250 Zeilen |
| Phase 2 | `belege/auto_namer.py` (Drive-Umbenennung) | ~80 Zeilen |
| Phase 2 | `belege/merger.py` (pypdf) | ~60 Zeilen |
| Phase 3 | MCP-Integration, Tracker-Sheet | ~100 Zeilen |

**Total:** ~940 Zeilen Code + Tests + Doku. Realistisch 2-3 Coding-Sessions.

---

**Nächster Schritt:** Marco reviewt diese Spec, klärt die 5 offenen
Fragen in Abschnitt 10, dann starte ich mit Phase 1 (Refactor + neuer
Beleg-Extraktor + Sheet-Generator).
