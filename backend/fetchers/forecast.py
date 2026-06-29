"""Read forecast data from Google Sheets.

Actual sheet structure (per product tab)
-----------------------------------------
Row 0:  col B = "Escenario Base"   (scenario title)
Row 1:  col B = "País", col C = "Proporcion país", col D = "Baseline",
        col E = "Inventario Actual",
        col F+ = month headers: "Feb", "Mar", … "Dec", "Jan/26", "Feb/26", … "Dec/26"
Row 2:  España   + monthly values in same row
Row 3:  Italia   + monthly values
Row 4:  Alemania + monthly values
Row 5:  Francia  + monthly values
Row 6:  Total    ← stop here

Countries are in column B (index 1).
Month values are in columns F+ (index 5+).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime

import pandas as pd

logger = logging.getLogger(__name__)

SHEET_ID    = os.getenv("GOOGLE_SHEET_ID", "10ED9_5s_UY_y2Eqs3LAt4xgyJaVQCIkqyAMYsfgnicI")
IGNORE_TABS = {"inputs crec mom"}

COUNTRY_MAP: dict[str, str] = {
    # Amazon EU core (synced via SP-API)
    "españa":         "ES",
    "espana":         "ES",
    "italia":         "IT",
    "alemania":       "DE",
    "francia":        "FR",
    # Amazon EU expansion (forecast-only)
    "holanda":        "NL",
    "países bajos":   "NL",
    "paises bajos":   "NL",
    "netherlands":    "NL",
    "bélgica":        "BE",
    "belgica":        "BE",
    "belgium":        "BE",
    "irlanda":        "IE",
    "ireland":        "IE",
    # Other channels
    "tiktok":         "TT",
    "tik tok":        "TT",
    "tt":             "TT",
}

_ES_MONTHS: dict[str, int] = {
    "ene":1,"feb":2,"mar":3,"abr":4,"may":5,"jun":6,
    "jul":7,"ago":8,"sep":9,"oct":10,"nov":11,"dic":12,
}
_EN_MONTHS: dict[str, int] = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}


# ── Auth ──────────────────────────────────────────────────────────

def _get_client():
    import gspread

    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if api_key:
        return gspread.api_key(api_key)

    creds_path = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if creds_path and os.path.exists(creds_path):
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(
            creds_path,
            scopes=["https://spreadsheets.google.com/feeds",
                    "https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        return gspread.authorize(creds)

    creds_content = os.getenv("GOOGLE_CREDENTIALS_JSON_CONTENT", "").strip()
    if creds_content:
        from google.oauth2.service_account import Credentials
        info = json.loads(creds_content)
        creds = Credentials.from_service_account_info(
            info,
            scopes=["https://spreadsheets.google.com/feeds",
                    "https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        return gspread.authorize(creds)

    raise ValueError(
        "No Google credentials found. Set GOOGLE_API_KEY or "
        "GOOGLE_CREDENTIALS_JSON in your .env file."
    )


# ── Parsing helpers ───────────────────────────────────────────────

def _parse_month_header(raw: str, assumed_year: int) -> str | None:
    """Convert a column header like 'Feb', 'Jan/26' → 'YYYY-MM'."""
    s = raw.strip()
    if not s:
        return None

    # Format: "Jan/26", "Feb/25", "dic/26"
    if "/" in s:
        parts      = s.split("/", 1)
        month_abbr = parts[0].strip().lower()[:3]
        year_part  = parts[1].strip()
        month_n    = _EN_MONTHS.get(month_abbr) or _ES_MONTHS.get(month_abbr)
        if month_n:
            try:
                year = int(year_part)
                if year < 100:
                    year += 2000
                return f"{year:04d}-{month_n:02d}"
            except ValueError:
                pass

    # Format: plain "Feb", "Mar", "dic" → use assumed_year
    abbr    = s.lower()[:3]
    month_n = _EN_MONTHS.get(abbr) or _ES_MONTHS.get(abbr)
    if month_n:
        return f"{assumed_year:04d}-{month_n:02d}"

    return None


def _clean_number(raw: str) -> float:
    s = raw.strip().replace("\xa0", "")
    if not s or s in ("-", "—", "N/A", "%"):
        return 0.0
    # Remove % signs
    s = s.replace("%", "")
    # European format: dots as thousands sep, comma as decimal
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_base_scenario(rows: list[list[str]]) -> pd.DataFrame | None:
    """Extract the Base Scenario table.

    Structure: rows = countries, columns = months.
    """
    # Step 1 — find the "Escenario Base" title row
    base_row_idx: int | None = None
    for i, row in enumerate(rows):
        for cell in row:
            if "escenario base" in cell.strip().lower():
                base_row_idx = i
                break
        if base_row_idx is not None:
            break

    if base_row_idx is None:
        logger.warning("'Escenario Base' not found")
        return None

    # Step 2 — the very next row is the header with month column names
    header_idx = base_row_idx + 1
    if header_idx >= len(rows):
        return None

    header_row  = rows[header_idx]
    today       = date.today()
    cur_year    = today.year

    # Build month_cols: col_index → 'YYYY-MM'
    # Two-pass month header parsing:
    # Plain headers like 'Feb','Mar' belong to the year BEFORE the first
    # explicit-year header (e.g. 'Jan/26'). Determine that year first.
    first_explicit_year : int | None = None
    first_explicit_month: int | None = None
    for cell in header_row:
        if "/" in cell.strip():
            ym = _parse_month_header(cell, cur_year)
            if ym:
                first_explicit_year  = int(ym[:4])
                first_explicit_month = int(ym[5:7])
                break

    if first_explicit_year and first_explicit_month == 1:
        plain_year = first_explicit_year - 1   # Jan/26 → plain months are 2025
    elif first_explicit_year:
        plain_year = first_explicit_year        # same year
    else:
        plain_year = cur_year

    month_cols: dict[int, str] = {}
    for j, cell in enumerate(header_row):
        ym = _parse_month_header(cell, plain_year if "/" not in cell else cur_year)
        if ym:
            month_cols[j] = ym

    if not month_cols:
        logger.warning("No month headers found in row %d", header_idx)
        return None

    # Step 3 — read country rows below the header
    # NOTE: we SKIP (continue) on "total"/"escenario" rows rather than breaking,
    # because the user puts new channels (TikTok, Holanda, Bélgica, Irlanda)
    # AFTER the Total row in the sheet. "tiktok" used to be a stop word but is
    # now a legitimate channel — we want to capture its row, not skip past it.
    COUNTRY_COL = 1          # Country names are in column B (index 1)
    SKIP_WORDS  = {"total", "prom", "escenario", "promedio"}

    # data: country_code → {month_key: value}
    country_rows: dict[str, dict[str, float]] = {}

    # Stop only when we've gone too far (e.g. empty rows after the last data row).
    consecutive_blanks = 0
    MAX_BLANKS = 5   # 5 blank rows in a row → end of table

    for row in rows[header_idx + 1:]:
        if not row or len(row) <= COUNTRY_COL:
            consecutive_blanks += 1
            if consecutive_blanks >= MAX_BLANKS:
                break
            continue

        name_raw = row[COUNTRY_COL].strip().lower().rstrip()
        if not name_raw:
            consecutive_blanks += 1
            if consecutive_blanks >= MAX_BLANKS:
                break
            continue

        consecutive_blanks = 0

        # Skip aggregate / placeholder rows (Total, Promedio, Escenario X)
        if any(name_raw.startswith(w) for w in SKIP_WORDS):
            continue

        code = COUNTRY_MAP.get(name_raw)
        if not code:
            continue

        vals: dict[str, float] = {}
        for col_idx, month_key in month_cols.items():
            raw = row[col_idx] if col_idx < len(row) else ""
            vals[month_key] = _clean_number(raw)
        country_rows[code] = vals

    if not country_rows:
        return None

    # Build DataFrame: index = month, columns = whatever country codes were found
    # (ES/FR/DE/IT for Amazon core, plus NL/BE/IE/TT if the sheet has them).
    all_months = sorted(set(m for v in country_rows.values() for m in v))
    all_codes  = sorted(country_rows.keys())
    records    = {m: {c: country_rows.get(c, {}).get(m, 0) for c in all_codes}
                  for m in all_months}
    df = pd.DataFrame(records).T.sort_index()
    df.index.name = "month"
    # Preferred display order: Amazon core first, then expansion, then channels
    preferred = ["ES", "FR", "DE", "IT", "NL", "BE", "IE", "TT"]
    cols = [c for c in preferred if c in df.columns] + \
           [c for c in df.columns if c not in preferred]
    return df[cols].round(0).astype(int)


# ── Public API ────────────────────────────────────────────────────

def fetch_forecast() -> dict[str, dict]:
    gc = _get_client()
    sh = gc.open_by_key(SHEET_ID)

    result: dict[str, dict] = {}
    for ws in sh.worksheets():
        tab = ws.title.strip()
        if tab.lower().strip() in IGNORE_TABS:
            continue
        # Skip non-product tabs
        if "_" not in tab:
            logger.info("forecast  skipping tab (no underscore): %s", tab)
            continue

        parts = tab.split("_", 1)
        asin  = parts[0].strip()
        label = parts[1].strip() if len(parts) > 1 else asin

        try:
            rows = ws.get_all_values()
            df   = _parse_base_scenario(rows)
            if df is not None and not df.empty:
                result[asin] = {"name": label, "tab": tab, "data": df}
                logger.info("forecast  %s → %d months", asin, len(df))
            else:
                logger.warning("forecast  no data in tab: %s", tab)
        except Exception as exc:
            logger.error("forecast  error in tab %s: %s", tab, exc)

    return result
