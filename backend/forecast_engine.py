"""Forecast engine — projects monthly unit sales per ASIN × marketplace.

Model (v1):
    baseline  = trailing 3-month average of real sales
    forecast  = baseline × (1 + growth%)^N for each future month N

Data sources:
    - Amazon markets (ES/FR/DE/IT): sales_velocity table (daily snapshots of
      rolling 30-day units sold). We pick ONE snapshot per month (the latest)
      to represent that month's sales.
    - TikTok (TT):                    tiktok_velocity table (velocity_daily
      × 30 for the window).

Growth %:
    - Default 5%/month (configurable via `forecast_settings` table per ASIN)
    - Represents expected month-over-month unit growth

Horizon:
    - Projects 12 months forward from current month

Output:
    - Writes to `forecast_computed` table (asin, marketplace, year_month, units)
    - Overwrites previous auto-generated rows (source='auto')
    - Does NOT touch rows marked source='manual_override' — user edits persist
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from collections import defaultdict

from backend.database import db_admin

logger = logging.getLogger(__name__)

MARKETS      = ["ES", "FR", "DE", "IT"]
HORIZON      = 12   # months to project forward
TRAILING_N   = 3    # months in baseline average
MIN_HISTORY  = 1    # need at least this many months of real data
DEFAULT_GROW = 5.0  # % month over month


# ── Helpers ────────────────────────────────────────────────────────────────

def _next_ym(year: int, month: int, offset: int) -> str:
    """Return 'YYYY-MM' offset months after (year, month)."""
    y = year + (month - 1 + offset) // 12
    m = (month - 1 + offset) % 12 + 1
    return f"{y:04d}-{m:02d}"


def _load_growth_settings() -> dict[str, float]:
    """Per-ASIN growth %; falls back to DEFAULT_GROW when missing."""
    try:
        rows = db_admin.table("forecast_settings").select("asin,growth_pct").execute().data or []
        return {r["asin"]: float(r["growth_pct"]) for r in rows if r.get("growth_pct") is not None}
    except Exception as exc:
        logger.warning("forecast_engine  settings load failed: %s", exc)
        return {}


# ── History extraction ────────────────────────────────────────────────────

def _monthly_history_amazon() -> dict[tuple, dict[str, int]]:
    """Return {(asin, marketplace): {ym: units_sold_30d}} using latest snapshot
    per (asin, mp, year_month) from sales_velocity.
    """
    rows = (
        db_admin.table("sales_velocity")
        .select("asin,marketplace,units_sold_30d,period_end_date")
        .order("period_end_date", desc=True)
        .execute().data or []
    )
    # Keep LATEST snapshot per (asin, mp, year_month)
    history: dict[tuple, dict[str, int]] = defaultdict(dict)
    seen: dict[tuple, str] = {}
    for r in rows:
        asin = r["asin"]
        mp   = r["marketplace"]
        if mp not in MARKETS:
            continue
        d = r.get("period_end_date") or ""
        ym = d[:7]
        if not ym:
            continue
        key = (asin, mp, ym)
        if key in seen:
            continue
        seen[key] = d
        history[(asin, mp)][ym] = int(r.get("units_sold_30d") or 0)
    return history


def _monthly_history_tiktok() -> dict[tuple, dict[str, int]]:
    """Return {(asin, 'TT'): {ym: units_sold_30d}} from tiktok_velocity.
    tiktok_velocity has a single row per ASIN (latest snapshot) so we only
    get the current month's figure, not a long history. That's OK — the
    growth model works with as little as 1 month of history."""
    try:
        rows = (
            db_admin.table("tiktok_velocity")
            .select("asin,units_sold_30d,period_end_date,velocity_daily")
            .execute().data or []
        )
    except Exception:
        return {}
    history: dict[tuple, dict[str, int]] = defaultdict(dict)
    for r in rows:
        asin = r["asin"]
        d = r.get("period_end_date") or ""
        ym = d[:7]
        if not ym:
            continue
        units_30d = int(r.get("units_sold_30d") or round(float(r.get("velocity_daily") or 0) * 30))
        history[(asin, "TT")][ym] = units_30d
    return history


# ── Forecasting ───────────────────────────────────────────────────────────

def _project(history_months: dict[str, int], growth_pct: float,
             horizon: int = HORIZON) -> list[tuple[str, int, float]]:
    """Project `horizon` future months from this history series.

    Returns list of (year_month, units, baseline_avg).
    - baseline = trailing N-month average (or whatever is available)
    - each next month = previous × (1 + growth_pct/100)
    """
    if not history_months:
        return []
    sorted_ym = sorted(history_months.keys())
    trailing  = [history_months[ym] for ym in sorted_ym[-TRAILING_N:]]
    baseline  = sum(trailing) / len(trailing)
    if baseline <= 0:
        baseline = max(trailing + [0])   # fall back to max in trailing
    if baseline <= 0:
        return []

    today = date.today()
    out: list[tuple[str, int, float]] = []
    cur = baseline
    factor = 1 + (growth_pct / 100.0)
    for i in range(1, horizon + 1):
        ym = _next_ym(today.year, today.month, i)
        cur = cur * factor
        out.append((ym, int(round(cur)), baseline))
    return out


# ── Public API ────────────────────────────────────────────────────────────

def regenerate_forecast(asin: str | None = None) -> tuple[int, list[str]]:
    """Regenerate projections and upsert to forecast_computed.
    If `asin` is given, only that product; otherwise all products with sales history.

    Does NOT overwrite rows marked source='manual_override'.
    Returns (n_rows_written, warnings).
    """
    warnings: list[str] = []

    settings = _load_growth_settings()
    amz_hist = _monthly_history_amazon()
    tt_hist  = _monthly_history_tiktok()

    history = {**amz_hist, **tt_hist}
    if asin:
        history = {k: v for k, v in history.items() if k[0] == asin}

    if not history:
        return 0, ["No sales history found — sales_velocity / tiktok_velocity empty?"]

    # Identify existing manual overrides to skip
    try:
        over_rows = (
            db_admin.table("forecast_computed")
            .select("asin,marketplace,year_month")
            .eq("source", "manual_override")
            .execute().data or []
        )
        overrides = {(r["asin"], r["marketplace"], r["year_month"]) for r in over_rows}
    except Exception:
        overrides = set()

    now_iso = datetime.now(timezone.utc).isoformat()
    to_upsert: list[dict] = []

    for (a, mp), months in history.items():
        growth = settings.get(a, DEFAULT_GROW)
        if len(months) < MIN_HISTORY:
            warnings.append(f"{a} {mp}: only {len(months)} month(s) history, skipped")
            continue
        projections = _project(months, growth)
        if not projections:
            warnings.append(f"{a} {mp}: baseline is zero, skipped")
            continue
        for ym, units, baseline in projections:
            if (a, mp, ym) in overrides:
                continue   # don't trample a user override
            to_upsert.append({
                "asin":          a,
                "marketplace":   mp,
                "year_month":    ym,
                "units":         units,
                "growth_pct":    round(growth, 2),
                "baseline_avg":  round(baseline, 2),
                "source":        "auto",
                "updated_at":    now_iso,
            })

    if not to_upsert:
        return 0, (warnings or ["Nothing to write (all months already overridden?)"])

    # Chunked upsert (Supabase has a payload size limit)
    written = 0
    for i in range(0, len(to_upsert), 500):
        chunk = to_upsert[i:i + 500]
        try:
            db_admin.table("forecast_computed").upsert(
                chunk, on_conflict="asin,marketplace,year_month"
            ).execute()
            written += len(chunk)
        except Exception as exc:
            warnings.append(f"Upsert chunk {i//500} failed: {exc}")

    logger.info("forecast_engine  wrote %d rows (%d ASIN×mp series)", written, len(history))
    return written, warnings


def load_forecast_table(asin: str | None = None) -> dict:
    """Convenience read for the dashboard — returns both history and
    projections for inspection. Keyed by (asin, mp) → {history, projections, growth_pct}.
    """
    amz = _monthly_history_amazon()
    tt  = _monthly_history_tiktok()
    hist = {**amz, **tt}
    if asin:
        hist = {k: v for k, v in hist.items() if k[0] == asin}

    settings = _load_growth_settings()

    try:
        fc_rows = db_admin.table("forecast_computed").select("*").execute().data or []
    except Exception:
        fc_rows = []
    fc_by_key: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for r in fc_rows:
        if asin and r["asin"] != asin:
            continue
        fc_by_key[(r["asin"], r["marketplace"])][r["year_month"]] = r

    out = {}
    for key in hist:
        a, mp = key
        out[key] = {
            "history":     dict(sorted(hist[key].items())),
            "projections": fc_by_key.get(key, {}),
            "growth_pct":  settings.get(a, DEFAULT_GROW),
        }
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    n, warns = regenerate_forecast()
    print(f"Wrote {n} rows")
    for w in warns[:10]:
        print(" ⚠", w)
