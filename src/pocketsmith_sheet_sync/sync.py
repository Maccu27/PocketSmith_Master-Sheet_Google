from __future__ import annotations

import logging
from datetime import date
from typing import Any

from .aggregator import AccountYearStats, aggregate_year
from .formatting import (
    COLOR_AUTO_BG,
    COLOR_HEADER_BG,
    COLOR_INPUT_BG,
    COLOR_NOTE_BG,
    COLOR_OK_BG,
    COLOR_WARN_BG,
    COLOR_ZEBRA,
    add_data_validation_checkbox_request,
    add_data_validation_dropdown_request,
    add_protected_range_request,
    cell_format,
    conditional_format_request,
    freeze_rows_request,
    header_format,
    repeat_cell_request,
    set_column_width_request,
)
from .pocketsmith import Account, PocketSmithClient
from .sheets import SheetsClient

log = logging.getLogger(__name__)

GERMAN_MONTHS = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]

KONTEN_TAB = "Konten"
UEBERSICHT_TAB = "Übersicht"
ANLEITUNG_TAB = "Anleitung"

KONTEN_HEADERS = [
    "Kontoname", "Aktiv", "Institut", "Währung",
    "Aktueller Saldo", "PocketSmith-ID",
    "Auszugsquelle", "PDF-Hinweis", "Notizen",
]
KONTEN_COL_AKTIV = 1  # B
KONTEN_COL_AUSZUGSQUELLE = 6  # G
KONTEN_AUSZUGSQUELLEN = ["Manuell", "PDF-Auto", "Keine"]

MONAT_HEADERS = [
    "Kontoname",                      # A  0  auto
    "Anzahl PocketSmith",             # B  1  auto, nach Split-Gruppierung
    "Soll-Anzahl",                    # C  2  manuell
    "Differenz Anzahl",               # D  3  formel: C - B
    "Saldo Monatsende",               # E  4  auto (laufender Monat: heutiger Stand)
    "Soll-Saldo",                     # F  5  manuell
    "Differenz Saldo",                # G  6  formel: F - E
    "Verifiziert",                    # H  7  auto: Gruppen, in denen ALLE Splits verifiziert sind
    "Verifiziert Prozent",            # I  8  formel: H / B
    "Gebucht",                        # J  9  manuell, checkbox
    "Notizen",                        # K 10  manuell
]
MONAT_COL_SOLL_ANZAHL = 2   # C
MONAT_COL_SOLL_SALDO = 5    # F
MONAT_COL_GEBUCHT = 9       # J
MONAT_COL_NOTIZEN = 10      # K


def monat_tab_name(year: int, month: int) -> str:
    return f"{GERMAN_MONTHS[month - 1]} {year}"


def col_letter(idx: int) -> str:
    """0-based index -> A, B, C, ..., AA, AB, ..."""
    s = ""
    n = idx
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def sync_year(
    ps: PocketSmithClient,
    gs: SheetsClient,
    *,
    spreadsheet_id: str,
    year: int,
    today: date,
    verified_label: str,
) -> None:
    log.info("loading accounts from PocketSmith")
    accounts = ps.list_accounts()
    log.info("got %d accounts", len(accounts))

    log.info("aggregating transactions for %d", year)
    stats_by_account: dict[int, AccountYearStats] = {}
    start = date(year, 1, 1)
    end = min(date(year, 12, 31), today)
    for acc in accounts:
        txs = list(ps.iter_transactions(acc.id, start_date=start, end_date=end))
        stats_by_account[acc.id] = aggregate_year(
            acc, txs, year=year, today=today, verified_label=verified_label,
        )
        log.debug("  %s: %d txs", acc.name, len(txs))

    log.info("ensuring tabs in spreadsheet")
    ensure_all_tabs(gs, spreadsheet_id, year)

    # Pro Jahres-Sheet zeigen wir nur Konten, die in dem Jahr Aktivität hatten.
    # Damit erscheinen Konten erst ab dem Jahr, in dem die erste Transaktion lief —
    # also z. B. DKB nicht in den 2003er Sheet, weil es erst 2021 in PocketSmith kam.
    accounts_in_year = [
        acc for acc in accounts
        if stats_by_account[acc.id].total_count_effective > 0
    ]
    log.info(
        "writing Konten tab (%d von %d accounts hatten Tx in %d)",
        len(accounts_in_year), len(accounts), year,
    )
    write_konten_tab(gs, spreadsheet_id, accounts_in_year, stats_by_account)

    log.info("writing Übersicht tab")
    write_uebersicht_tab(gs, spreadsheet_id, year, stats_by_account)

    log.info("reading active-flag from Konten tab")
    active_account_names = read_active_account_names(gs, spreadsheet_id)
    log.debug("active accounts: %s", active_account_names)

    for month in range(1, 13):
        log.info("writing %s tab", monat_tab_name(year, month))
        write_monat_tab(
            gs,
            spreadsheet_id,
            year=year,
            month=month,
            stats_by_account=stats_by_account,
            active_account_names=active_account_names,
        )

    log.info("writing Anleitung tab")
    write_anleitung_tab(gs, spreadsheet_id, year)

    log.info("cleaning up default empty tab if present")
    gs.delete_default_blank_tab(spreadsheet_id)


def ensure_all_tabs(gs: SheetsClient, spreadsheet_id: str, year: int) -> None:
    gs.ensure_tab(spreadsheet_id, UEBERSICHT_TAB, index=0, rows=80, cols=10)
    gs.ensure_tab(spreadsheet_id, KONTEN_TAB, index=1, rows=300, cols=15)
    for m in range(1, 13):
        gs.ensure_tab(spreadsheet_id, monat_tab_name(year, m), index=1 + m, rows=200, cols=15)
    gs.ensure_tab(spreadsheet_id, ANLEITUNG_TAB, index=14, rows=80, cols=4)


# ---------- Konten tab ----------

def read_konten_tab(gs: SheetsClient, spreadsheet_id: str) -> dict[str, dict[str, Any]]:
    """Read existing rows by Kontoname → preserve user-edited cells."""
    range_a1 = f"{KONTEN_TAB}!A1:I"
    try:
        result = gs._sheets.spreadsheets().values().get(  # noqa: SLF001
            spreadsheetId=spreadsheet_id, range=range_a1
        ).execute()
    except Exception:
        return {}
    values = result.get("values") or []
    if len(values) < 2:
        return {}
    headers = values[0]
    rows: dict[str, dict[str, Any]] = {}
    for row in values[1:]:
        if not row or not row[0]:
            continue
        name = row[0]
        rows[name] = {h: (row[i] if i < len(row) else "") for i, h in enumerate(headers)}
    return rows


def read_active_account_names(gs: SheetsClient, spreadsheet_id: str) -> set[str]:
    existing = read_konten_tab(gs, spreadsheet_id)
    active: set[str] = set()
    for name, row in existing.items():
        v = row.get("Aktiv", "")
        if isinstance(v, bool):
            if v:
                active.add(name)
        else:
            if str(v).strip().upper() in ("TRUE", "WAHR", "1", "YES", "JA"):
                active.add(name)
    return active


def write_konten_tab(
    gs: SheetsClient,
    spreadsheet_id: str,
    accounts: list[Account],
    stats_by_account: dict[int, AccountYearStats],
) -> None:
    existing = read_konten_tab(gs, spreadsheet_id)
    is_first_run = not existing  # Konten-Tab war leer → Smart-Default

    accounts_sorted = sorted(accounts, key=lambda a: a.name.lower())

    rows: list[list[Any]] = [KONTEN_HEADERS]
    for acc in accounts_sorted:
        prev = existing.get(acc.name, {})
        aktiv_prev = prev.get("Aktiv", "")
        aktiv_value: bool
        if isinstance(aktiv_prev, bool):
            aktiv_value = aktiv_prev
        elif aktiv_prev != "":
            aktiv_value = str(aktiv_prev).strip().upper() in ("TRUE", "WAHR", "1", "YES", "JA")
        elif is_first_run:
            # Beim ersten Lauf: Konto wird auto-aktiviert, wenn es im Jahr
            # mindestens eine Transaktion hatte. Spart manuelles Anhaken bei
            # neuen Jahres-Sheets.
            stats = stats_by_account.get(acc.id)
            aktiv_value = bool(stats and stats.total_count_effective > 0)
        else:
            aktiv_value = False
        rows.append([
            acc.name,
            aktiv_value,
            acc.institution or "",
            acc.currency,
            acc.current_balance,
            acc.id,
            prev.get("Auszugsquelle") or "Manuell",
            prev.get("PDF-Hinweis") or "",
            prev.get("Notizen") or "",
        ])

    last_col = col_letter(len(KONTEN_HEADERS) - 1)
    last_row = len(rows)
    gs.clear_range(spreadsheet_id, f"{KONTEN_TAB}!A1:{last_col}1000")
    gs.write_values(spreadsheet_id, f"{KONTEN_TAB}!A1:{last_col}{last_row}", rows)

    sheet_id = gs.list_tabs(spreadsheet_id)[KONTEN_TAB]
    gs.clear_protections_and_conditional_formats(spreadsheet_id, sheet_id)
    apply_konten_formatting(gs, spreadsheet_id, sheet_id, data_row_count=last_row - 1)


def apply_konten_formatting(
    gs: SheetsClient, spreadsheet_id: str, sheet_id: int, *, data_row_count: int
) -> None:
    requests: list[dict[str, Any]] = []

    # Header row
    requests.append(repeat_cell_request(
        sheet_id, start_row=0, end_row=1, start_col=0, end_col=len(KONTEN_HEADERS),
        cell_format_data=header_format(),
    ))
    requests.append(freeze_rows_request(sheet_id, 1))

    # Auto-filled columns: A (Kontoname), C (Institut), D (Währung), E (Saldo), F (ID)
    for col in (0, 2, 3, 4, 5):
        requests.append(repeat_cell_request(
            sheet_id, start_row=1, end_row=1 + data_row_count,
            start_col=col, end_col=col + 1,
            cell_format_data=cell_format(background=COLOR_AUTO_BG),
        ))

    # Currency formatting on E (Aktueller Saldo)
    requests.append(repeat_cell_request(
        sheet_id, start_row=1, end_row=1 + data_row_count,
        start_col=4, end_col=5,
        cell_format_data=cell_format(
            background=COLOR_AUTO_BG,
            number_format={"type": "NUMBER", "pattern": "#,##0.00"},
            horizontal_alignment="RIGHT",
        ),
    ))

    # User-input columns: B (Aktiv), G (Auszugsquelle), H (PDF-Hinweis), I (Notizen)
    for col in (1, 6, 7, 8):
        requests.append(repeat_cell_request(
            sheet_id, start_row=1, end_row=1 + data_row_count,
            start_col=col, end_col=col + 1,
            cell_format_data=cell_format(background=COLOR_INPUT_BG),
        ))

    # Notizen wieder weiß (unterscheidet sich von Pflichtfeldern)
    requests.append(repeat_cell_request(
        sheet_id, start_row=1, end_row=1 + data_row_count,
        start_col=8, end_col=9,
        cell_format_data=cell_format(background=COLOR_NOTE_BG),
    ))

    # Aktiv → Checkbox
    requests.append(add_data_validation_checkbox_request(
        sheet_id, start_row=1, end_row=1 + data_row_count, start_col=1, end_col=2,
    ))

    # Auszugsquelle → Dropdown
    requests.append(add_data_validation_dropdown_request(
        sheet_id, start_row=1, end_row=1 + data_row_count,
        start_col=6, end_col=7, values=KONTEN_AUSZUGSQUELLEN,
    ))

    # Geschützte Bereiche: alles außer Aktiv/Auszugsquelle/PDF-Hinweis/Notizen
    # Header-Zeile
    requests.append(add_protected_range_request(
        sheet_id, start_row=0, end_row=1, start_col=0, end_col=len(KONTEN_HEADERS),
        description="Header (vom Script verwaltet)",
    ))
    # Auto-Spalten A, C, D, E, F
    for col in (0, 2, 3, 4, 5):
        requests.append(add_protected_range_request(
            sheet_id, start_row=1, end_row=1 + data_row_count,
            start_col=col, end_col=col + 1,
            description=f"Auto-Spalte {KONTEN_HEADERS[col]} (vom Script verwaltet)",
        ))

    # Spaltenbreiten
    widths = [220, 60, 180, 70, 110, 110, 130, 180, 250]
    for i, w in enumerate(widths):
        requests.append(set_column_width_request(sheet_id, i, i + 1, w))

    gs.batch_update(spreadsheet_id, requests)


# ---------- Übersicht tab ----------

def write_uebersicht_tab(
    gs: SheetsClient,
    spreadsheet_id: str,
    year: int,
    stats_by_account: dict[int, AccountYearStats],
) -> None:
    total = sum(s.total_count_effective for s in stats_by_account.values())
    total_verified = sum(s.total_verified_effective for s in stats_by_account.values())

    rows: list[list[Any]] = []
    rows.append([f"Master {year} – Übersicht"])
    rows.append([])
    rows.append(["Jahresübersicht (Anzahl nach Split-Gruppierung)"])
    rows.append(["Jahr", "Transaktionen Gesamt", "Davon Verifiziert", "Verifiziert Prozent"])
    rows.append([
        year,
        total,
        total_verified,
        f"=IFERROR(C5/B5;0)",
    ])
    rows.append([])
    rows.append(["Monatsübersicht (Anzahl nach Split-Gruppierung)"])
    rows.append(["Monat", "Transaktionen Gesamt", "Davon Verifiziert", "Verifiziert Prozent"])

    for m in range(1, 13):
        c = sum(s.months[m].count_effective for s in stats_by_account.values())
        cv = sum(s.months[m].count_verified_effective for s in stats_by_account.values())
        formula_row = 8 + m  # 1-basiert, Header auf Zeile 8
        rows.append([
            GERMAN_MONTHS[m - 1],
            c,
            cv,
            f"=IFERROR(C{formula_row}/B{formula_row};0)",
        ])

    rows.append([])
    rows.append(["Farb-Legende"])
    rows.append(["⬜ Hellgrau", "vom Script befüllt – nicht reinschreiben"])
    rows.append(["🟡 Hellgelb", "manuell von dir auszufüllen"])
    rows.append(["🟢 Hellgrün", "alles stimmt (Diff = 0)"])
    rows.append(["🟠 Hellorange", "Differenz ≠ 0 – prüfen"])
    rows.append(["⬜ Weiß", "freier Notizbereich"])

    gs.clear_range(spreadsheet_id, f"{UEBERSICHT_TAB}!A1:Z200")
    gs.write_values(spreadsheet_id, f"{UEBERSICHT_TAB}!A1", rows)

    sheet_id = gs.list_tabs(spreadsheet_id)[UEBERSICHT_TAB]
    gs.clear_protections_and_conditional_formats(spreadsheet_id, sheet_id)
    apply_uebersicht_formatting(gs, spreadsheet_id, sheet_id)


def apply_uebersicht_formatting(gs: SheetsClient, spreadsheet_id: str, sheet_id: int) -> None:
    requests: list[dict[str, Any]] = []

    # Titel (row 1)
    requests.append(repeat_cell_request(
        sheet_id, start_row=0, end_row=1, start_col=0, end_col=4,
        cell_format_data=cell_format(
            background=COLOR_HEADER_BG,
            bold=True,
        ) | {"textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True, "fontSize": 12}},
    ))

    # Sektion-Header (row 3 = "Jahresübersicht", row 7 = "Monatsübersicht", row 22 = "Farb-Legende")
    for r in (2, 6, 21):
        requests.append(repeat_cell_request(
            sheet_id, start_row=r, end_row=r + 1, start_col=0, end_col=4,
            cell_format_data=cell_format(bold=True, background=COLOR_AUTO_BG),
        ))

    # Tabellen-Header (row 4, row 8)
    for r in (3, 7):
        requests.append(repeat_cell_request(
            sheet_id, start_row=r, end_row=r + 1, start_col=0, end_col=4,
            cell_format_data=header_format(),
        ))

    # Daten (row 5 = Jahres-Total, rows 9-20 = Monate)
    requests.append(repeat_cell_request(
        sheet_id, start_row=4, end_row=5, start_col=0, end_col=4,
        cell_format_data=cell_format(background=COLOR_AUTO_BG),
    ))
    requests.append(repeat_cell_request(
        sheet_id, start_row=8, end_row=20, start_col=0, end_col=4,
        cell_format_data=cell_format(background=COLOR_AUTO_BG),
    ))

    # Prozent-Spalte (D, idx 3)
    pct_format = cell_format(
        background=COLOR_AUTO_BG,
        number_format={"type": "PERCENT", "pattern": "0.0%"},
        horizontal_alignment="RIGHT",
    )
    requests.append(repeat_cell_request(
        sheet_id, start_row=4, end_row=5, start_col=3, end_col=4,
        cell_format_data=pct_format,
    ))
    requests.append(repeat_cell_request(
        sheet_id, start_row=8, end_row=20, start_col=3, end_col=4,
        cell_format_data=pct_format,
    ))

    # Spaltenbreiten
    for i, w in enumerate([180, 200, 200, 140]):
        requests.append(set_column_width_request(sheet_id, i, i + 1, w))

    # Header-Zeile-Schutz
    requests.append(add_protected_range_request(
        sheet_id, start_row=0, end_row=21, start_col=0, end_col=4,
        description="Übersicht (vom Script verwaltet)",
    ))

    gs.batch_update(spreadsheet_id, requests)


# ---------- Monatstabs ----------

def read_monat_user_columns(
    gs: SheetsClient, spreadsheet_id: str, tab: str
) -> dict[str, dict[str, Any]]:
    range_a1 = f"{tab}!A1:K"
    try:
        result = gs._sheets.spreadsheets().values().get(  # noqa: SLF001
            spreadsheetId=spreadsheet_id, range=range_a1
        ).execute()
    except Exception:
        return {}
    values = result.get("values") or []
    if len(values) < 2:
        return {}
    headers = values[0]
    rows: dict[str, dict[str, Any]] = {}
    for row in values[1:]:
        if not row or not row[0]:
            continue
        name = row[0]
        rows[name] = {h: (row[i] if i < len(row) else "") for i, h in enumerate(headers)}
    return rows


def write_monat_tab(
    gs: SheetsClient,
    spreadsheet_id: str,
    *,
    year: int,
    month: int,
    stats_by_account: dict[int, AccountYearStats],
    active_account_names: set[str],
) -> None:
    tab = monat_tab_name(year, month)
    existing = read_monat_user_columns(gs, spreadsheet_id, tab)

    active_stats = [
        s for s in stats_by_account.values() if s.account.name in active_account_names
    ]
    active_stats.sort(key=lambda s: s.account.name.lower())

    rows: list[list[Any]] = [MONAT_HEADERS]
    for s in active_stats:
        ms = s.months[month]
        prev = existing.get(s.account.name, {})
        soll_count = prev.get("Soll-Anzahl") or ""
        soll_balance = prev.get("Soll-Saldo") or ""
        gebucht_prev = prev.get("Gebucht", "")
        if isinstance(gebucht_prev, bool):
            gebucht_value: bool = gebucht_prev
        else:
            gebucht_value = str(gebucht_prev).strip().upper() in ("TRUE", "WAHR", "1", "YES", "JA")
        notizen = prev.get("Notizen") or ""

        rows.append([
            s.account.name,                                              # A
            ms.count_effective,                                          # B  Anzahl PocketSmith (netto)
            soll_count,                                                  # C  Soll-Anzahl
            "",                                                          # D  Differenz Anzahl (Formel)
            ms.end_of_month_balance if ms.end_of_month_balance is not None else "",  # E
            soll_balance,                                                # F  Soll-Saldo
            "",                                                          # G  Differenz Saldo (Formel)
            ms.count_verified_effective,                                 # H  Verifiziert
            "",                                                          # I  Verifiziert Prozent (Formel)
            gebucht_value,                                               # J  Gebucht
            notizen,                                                     # K  Notizen
        ])

    # Formeln einsetzen (Zeilen-Index 1-basiert, Header = Zeile 1)
    for i in range(1, len(rows)):
        sheet_row = i + 1
        # Differenz Anzahl = Soll-Anzahl(C) − Anzahl PocketSmith(B)
        rows[i][3] = f"=IFERROR(C{sheet_row}-B{sheet_row};\"\")"
        # Differenz Saldo = Soll-Saldo(F) − Saldo Monatsende(E)
        rows[i][6] = f"=IFERROR(F{sheet_row}-E{sheet_row};\"\")"
        # Verifiziert Prozent = Verifiziert(H) / Anzahl PocketSmith(B)
        rows[i][8] = f"=IFERROR(H{sheet_row}/B{sheet_row};0)"

    last_col = col_letter(len(MONAT_HEADERS) - 1)
    last_row = len(rows)
    gs.clear_range(spreadsheet_id, f"{tab}!A1:{last_col}1000")
    gs.write_values(spreadsheet_id, f"{tab}!A1:{last_col}{last_row}", rows)

    sheet_id = gs.list_tabs(spreadsheet_id)[tab]
    gs.clear_protections_and_conditional_formats(spreadsheet_id, sheet_id)
    apply_monat_formatting(gs, spreadsheet_id, sheet_id, data_row_count=last_row - 1)


def apply_monat_formatting(
    gs: SheetsClient, spreadsheet_id: str, sheet_id: int, *, data_row_count: int
) -> None:
    """
    Spaltenlayout (0-basiert):
       0 A Kontoname             auto
       1 B Anzahl PocketSmith    auto (nach Split-Gruppierung)
       2 C Soll-Anzahl           manuell (gelb)
       3 D Differenz Anzahl      auto (Formel)
       4 E Saldo Monatsende      auto
       5 F Soll-Saldo            manuell (gelb)
       6 G Differenz Saldo       auto (Formel)
       7 H Verifiziert           auto
       8 I Verifiziert Prozent   auto (Formel)
       9 J Gebucht               manuell (gelb, Checkbox)
      10 K Notizen               manuell (weiß)
    """
    requests: list[dict[str, Any]] = []
    n_cols = len(MONAT_HEADERS)

    AUTO_COLS = (0, 1, 3, 4, 6, 7, 8)
    INPUT_GELB_COLS = (2, 5)             # Soll-Anzahl, Soll-Saldo
    GEBUCHT_COL = 9
    NOTIZEN_COL = 10

    # Header
    requests.append(repeat_cell_request(
        sheet_id, start_row=0, end_row=1, start_col=0, end_col=n_cols,
        cell_format_data=header_format(),
    ))
    requests.append(freeze_rows_request(sheet_id, 1))

    if data_row_count == 0:
        gs.batch_update(spreadsheet_id, requests)
        return

    # Auto-Spalten: hellgrau
    for col in AUTO_COLS:
        requests.append(repeat_cell_request(
            sheet_id, start_row=1, end_row=1 + data_row_count,
            start_col=col, end_col=col + 1,
            cell_format_data=cell_format(background=COLOR_AUTO_BG),
        ))
    # E: Saldo Monatsende → Zahlenformat
    requests.append(repeat_cell_request(
        sheet_id, start_row=1, end_row=1 + data_row_count, start_col=4, end_col=5,
        cell_format_data=cell_format(
            background=COLOR_AUTO_BG,
            number_format={"type": "NUMBER", "pattern": "#,##0.00"},
            horizontal_alignment="RIGHT",
        ),
    ))
    # G: Differenz Saldo → Zahlenformat (rot bei Minus)
    requests.append(repeat_cell_request(
        sheet_id, start_row=1, end_row=1 + data_row_count, start_col=6, end_col=7,
        cell_format_data=cell_format(
            background=COLOR_AUTO_BG,
            number_format={"type": "NUMBER", "pattern": "#,##0.00;[red]-#,##0.00"},
            horizontal_alignment="RIGHT",
        ),
    ))
    # I: Verifiziert Prozent → Prozent
    requests.append(repeat_cell_request(
        sheet_id, start_row=1, end_row=1 + data_row_count, start_col=8, end_col=9,
        cell_format_data=cell_format(
            background=COLOR_AUTO_BG,
            number_format={"type": "PERCENT", "pattern": "0.0%"},
            horizontal_alignment="RIGHT",
        ),
    ))

    # Manuelle Eingabespalten gelb
    for col in INPUT_GELB_COLS:
        nf = {"type": "NUMBER", "pattern": "#,##0.00"} if col == 5 else None
        requests.append(repeat_cell_request(
            sheet_id, start_row=1, end_row=1 + data_row_count,
            start_col=col, end_col=col + 1,
            cell_format_data=cell_format(
                background=COLOR_INPUT_BG,
                number_format=nf,
                horizontal_alignment="RIGHT",
            ),
        ))

    # J: Gebucht → Checkbox auf gelbem Grund
    requests.append(repeat_cell_request(
        sheet_id, start_row=1, end_row=1 + data_row_count,
        start_col=GEBUCHT_COL, end_col=GEBUCHT_COL + 1,
        cell_format_data=cell_format(background=COLOR_INPUT_BG, horizontal_alignment="CENTER"),
    ))
    requests.append(add_data_validation_checkbox_request(
        sheet_id, start_row=1, end_row=1 + data_row_count,
        start_col=GEBUCHT_COL, end_col=GEBUCHT_COL + 1,
    ))

    # K: Notizen weiß
    requests.append(repeat_cell_request(
        sheet_id, start_row=1, end_row=1 + data_row_count,
        start_col=NOTIZEN_COL, end_col=NOTIZEN_COL + 1,
        cell_format_data=cell_format(background=COLOR_NOTE_BG),
    ))

    # Conditional formatting: Differenz Anzahl ist jetzt Spalte D (Index 3)
    requests.append(conditional_format_request(
        sheet_id, start_row=1, end_row=1 + data_row_count, start_col=3, end_col=4,
        formula="=AND(ISNUMBER(D2);D2=0)", background=COLOR_OK_BG, index=0,
    ))
    requests.append(conditional_format_request(
        sheet_id, start_row=1, end_row=1 + data_row_count, start_col=3, end_col=4,
        formula="=AND(ISNUMBER(D2);D2<>0)", background=COLOR_WARN_BG, index=1,
    ))
    # Differenz Saldo ist jetzt Spalte G (Index 6)
    requests.append(conditional_format_request(
        sheet_id, start_row=1, end_row=1 + data_row_count, start_col=6, end_col=7,
        formula="=AND(ISNUMBER(G2);ROUND(G2;2)=0)", background=COLOR_OK_BG, index=2,
    ))
    requests.append(conditional_format_request(
        sheet_id, start_row=1, end_row=1 + data_row_count, start_col=6, end_col=7,
        formula="=AND(ISNUMBER(G2);ROUND(G2;2)<>0)", background=COLOR_WARN_BG, index=3,
    ))

    # Spaltenbreiten — 11 Werte
    widths = [220, 130, 110, 130, 140, 120, 130, 110, 140, 90, 240]
    for i, w in enumerate(widths):
        requests.append(set_column_width_request(sheet_id, i, i + 1, w))

    # Schutz: Header + Auto-Spalten
    requests.append(add_protected_range_request(
        sheet_id, start_row=0, end_row=1, start_col=0, end_col=n_cols,
        description="Header (vom Script verwaltet)",
    ))
    for col in AUTO_COLS:
        requests.append(add_protected_range_request(
            sheet_id, start_row=1, end_row=1 + data_row_count,
            start_col=col, end_col=col + 1,
            description=f"Auto-Spalte {MONAT_HEADERS[col]} (vom Script verwaltet)",
        ))

    gs.batch_update(spreadsheet_id, requests)


# ---------- Anleitung tab ----------

ANLEITUNG_INHALT: list[list[str]] = [
    [f"Anleitung – Master Sheet"],
    [],
    ["Diese Tabelle wird täglich um 05:00 Uhr automatisch aus PocketSmith aktualisiert."],
    ["Du musst nichts manuell synchronisieren."],
    [],
    ["────────────────────────────────────────────────────────────────"],
    ["Neues Jahr hinzufügen (z. B. 2025 oder 2024)"],
    ["────────────────────────────────────────────────────────────────"],
    [],
    ["Schritt 1 – Neue Sheet in Google Drive anlegen"],
    ["  • In Drive: leere Google Sheet erstellen, sinnvoll benennen (z. B. \"Master 2025\")"],
    ["  • Sheet-ID notieren (steht in der URL: docs.google.com/spreadsheets/d/SHEET-ID/edit)"],
    ["  • Sheet teilen (oben rechts \"Freigeben\")"],
    ["  • Service-Account-Email als Editor hinzufügen:"],
    ["    pocketsmith-sync@master-haven-494414-g0.iam.gserviceaccount.com"],
    [],
    ["Schritt 2 – Railway konfigurieren"],
    ["  • railway.app öffnen → Projekt \"pocketsmith-sheet-sync\" → Variables"],
    ["  • Neue Variable hinzufügen:"],
    ["    Name:  MASTER_SHEET_<JAHR>     (z. B. MASTER_SHEET_2025)"],
    ["    Value: <Sheet-ID aus Schritt 1>"],
    ["  • Existierende Variable SYNC_YEARS erweitern:"],
    ["    SYNC_YEARS=2026,2025          (komma-getrennt, ohne Leerzeichen)"],
    [],
    ["Schritt 3 – Sync auslösen"],
    ["  • Railway-Projekt → Deployments → \"Redeploy\" beim letzten Deployment"],
    ["  • Oder einfach bis 05:00 Uhr nächster Tag warten"],
    ["  • Nach ~2 Min ist die neue Sheet komplett befüllt:"],
    ["    Übersicht, Konten, Januar–Dezember, Anleitung"],
    [],
    ["Schritt 4 – Aktiv-Häkchen prüfen (optional)"],
    ["  • Konten-Tab in der neuen Sheet öffnen"],
    ["  • Das Skript hat die Aktiv-Häkchen automatisch für Konten gesetzt,"],
    ["    die im Jahr mindestens 1 Transaktion hatten"],
    ["  • Bei Bedarf einzelne Häkchen entfernen oder hinzufügen"],
    [],
    ["────────────────────────────────────────────────────────────────"],
    ["Was du in der Sheet selbst machen darfst"],
    ["────────────────────────────────────────────────────────────────"],
    [],
    ["  ✓ Manuelle Eingaben in gelben Feldern (Soll-Anzahl, Soll-Saldo)"],
    ["  ✓ Aktiv-Häkchen in der Konten-Tab setzen/entfernen"],
    ["  ✓ Gebucht-Häkchen in den Monats-Tabs"],
    ["  ✓ Eigene Einträge in der Notizen-Spalte (wird nie überschrieben)"],
    ["  ✓ Eigene Tabs hinzufügen (werden vom Skript ignoriert)"],
    ["  ✓ Layout, Farben, Spaltenbreiten ändern"],
    [],
    ["  ✗ Spalten umsortieren oder umbenennen (bricht Skript)"],
    ["  ✗ Tabs umbenennen (Skript findet sie dann nicht mehr)"],
    [],
    ["────────────────────────────────────────────────────────────────"],
    ["Bei Fragen oder Problemen"],
    ["────────────────────────────────────────────────────────────────"],
    [],
    ["  • Logs: railway.app → Projekt → Deployments → letzter Eintrag → Logs"],
    ["  • Code: github.com/Maccu27/PocketSmith_Master-Sheet_Google"],
    ["  • Service-Account: pocketsmith-sync@master-haven-494414-g0.iam.gserviceaccount.com"],
]


def write_anleitung_tab(gs: SheetsClient, spreadsheet_id: str, year: int) -> None:
    rows = [list(r) for r in ANLEITUNG_INHALT]
    gs.clear_range(spreadsheet_id, f"{ANLEITUNG_TAB}!A1:Z200")
    gs.write_values(spreadsheet_id, f"{ANLEITUNG_TAB}!A1", rows)

    sheet_id = gs.list_tabs(spreadsheet_id)[ANLEITUNG_TAB]
    gs.clear_protections_and_conditional_formats(spreadsheet_id, sheet_id)
    apply_anleitung_formatting(gs, spreadsheet_id, sheet_id, total_rows=len(rows))


def apply_anleitung_formatting(
    gs: SheetsClient, spreadsheet_id: str, sheet_id: int, *, total_rows: int
) -> None:
    requests: list[dict[str, Any]] = []

    # Titel-Zeile (row 0): wie Header-Style, aber etwas größer
    requests.append(repeat_cell_request(
        sheet_id, start_row=0, end_row=1, start_col=0, end_col=4,
        cell_format_data={
            "backgroundColor": COLOR_HEADER_BG,
            "horizontalAlignment": "LEFT",
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
            "textFormat": {
                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                "fontSize": 12,
                "bold": True,
            },
            "padding": {"top": 6, "bottom": 6, "left": 8, "right": 6},
        },
    ))

    # Sektionen-Header (Zeilen 6, 36, 49 in 1-basiert → 0-basiert 5, 35, 48)
    # Wir suchen nach "─" als Marker
    section_header_rows: list[int] = []
    for i, row in enumerate(ANLEITUNG_INHALT):
        if row and isinstance(row[0], str) and "─" in row[0]:
            # die Zeile danach ist der Sektion-Titel
            section_header_rows.append(i + 1)

    for r in section_header_rows:
        requests.append(repeat_cell_request(
            sheet_id, start_row=r, end_row=r + 1, start_col=0, end_col=4,
            cell_format_data=cell_format(bold=True, background=COLOR_AUTO_BG),
        ))

    # Komplette Spalte A breit, damit alles lesbar bleibt
    requests.append(set_column_width_request(sheet_id, 0, 1, 800))

    # Schutz: alles
    requests.append(add_protected_range_request(
        sheet_id, start_row=0, end_row=total_rows + 5, start_col=0, end_col=4,
        description="Anleitung (vom Script verwaltet)",
    ))

    gs.batch_update(spreadsheet_id, requests)
