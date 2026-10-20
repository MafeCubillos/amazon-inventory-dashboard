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


def _load_growth_settings() -> dict[tuple[str, str], float]:
    """Per-(asin, marketplace) growth %. The special marketplace 'ALL' is the
    product-level fallback (used if no per-country override exists).
    """
    try:
        rows = db_admin.table("forecast_settings").select(
            "asin,marketplace,growth_pct"
        ).execute().data or []
        out = {}
        for r in rows:
            if r.get("growth_pct") is None:
                continue
            mp = r.get("marketplace") or "ALL"
            out[(r["asin"], mp)] = float(r["growth_pct"])
        return out
    except Exception as exc:
        logger.warning("forecast_engine  settings load failed: %s", exc)
        return {}


def _resolve_growth(
    settings: dict[tuple[str, str], float],
    asin: str,
    marketplace: str,
) -> float:
    """Resolve the growth % for (asin, marketplace), falling back to:
       (asin, 'ALL') → product-wide default → DEFAULT_GROW.
    """
    if (asin, marketplace) in settings:
        return settings[(asin, marketplace)]
    if (asin, "ALL") in settings:
        return settings[(asin, "ALL")]
    return DEFAULT_GROW


def _suggest_growth_from_history(months: dict[str, int]) -> float | None:
    """Compute a data-driven growth suggestion as the geometric mean of
    month-over-month ratios in the last 6 months (ignoring zeros).

    Returns a percent (e.g. 5.0 = 5% monthly growth). None if not enough data
    or if numbers are too volatile to be meaningful.
    """
    if not months or len(months) < 3:
        return None
    sorted_ym = sorted(months.keys())
    series = [months[ym] for ym in sorted_ym[-6:]]
    # Drop the most recent partial month if it looks much smaller than the
    # trailing — common when today is mid-month and only part of sales is in.
    if len(series) >= 2 and series[-1] < 0.5 * (sum(series[:-1]) / len(series[:-1])):
        series = series[:-1]
    if len(series) < 2:
        return None
    # Month-over-month ratios; skip pairs where previous is 0
    ratios: list[float] = []
    for a, b in zip(series[:-1], series[1:]):
        if a <= 0:
            continue
        ratios.append(b / a)
    if not ratios:
        return None
    # Geometric mean of ratios → average monthly growth factor
    product = 1.0
    for r in ratios:
        product *= r
    gm = product ** (1.0 / len(ratios))
    pct = (gm - 1.0) * 100.0
    # Clamp to a sane range so a single outlier doesn't produce 400%/mo
    return max(-30.0, min(50.0, round(pct, 1)))


# ── History extraction ────────────────────────────────────────────────────

def _monthly_history_from_sales_monthly() -> dict[tuple, dict[str, int]]:
    """Preferred source: calendar-month aggregates stored in sales_monthly.
    One row per (asin, marketplace, year_month) — accurate per-calendar-month
    totals from SP-API's Sales & Traffic report.
    """
    history: dict[tuple, dict[str, int]] = defaultdict(dict)
    try:
        rows: list[dict] = []
        page = 0
        page_size = 1000
        while True:
            chunk = (
                db_admin.table("sales_monthly")
                .select("asin,marketplace,year_month,units")
                .range(page * page_size, (page + 1) * page_size - 1)
                .execute().data or []
            )
            rows.extend(chunk)
            if len(chunk) < page_size:
                break
            page += 1
            if page > 20:
                break
    except Exception as exc:
        logger.warning("forecast_engine  sales_monthly read failed: %s", exc)
        return history

    for r in rows:
        mp = r["marketplace"]
        if mp not in MARKETS:
            continue
        history[(r["asin"], mp)][r["year_month"]] = int(r.get("units") or 0)
    return history


def _monthly_history_amazon(months_back: int = 12) -> dict[tuple, dict[str, int]]:
    """Return {(asin, marketplace): {ym: units_sold_30d}}.

    Prefers sales_monthly (calendar-month aggregates). Falls back to the
    latest sales_velocity snapshot per month for any (asin, mp, ym) that
    sales_monthly doesn't cover yet.

    Queries one month at a time to avoid the Supabase 1000-row cap.
    """
    import calendar as _cal
    from datetime import date as _date

    today = _date.today()
    # Build list of (year, month) for the last `months_back` months, oldest first
    months: list[tuple[int, int]] = []
    y, m = today.year, today.month
    for _ in range(months_back + 1):   # include current month
        months.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    months.reverse()

    # Start from the accurate monthly aggregates; sales_velocity fills gaps
    history: dict[tuple, dict[str, int]] = defaultdict(dict)
    for (k, mv) in _monthly_history_from_sales_monthly().items():
        history[k] = dict(mv)   # copy so defaultdict stays usable

    for (yy, mm) in months:
        ym = f"{yy:04d}-{mm:02d}"
        first_day = f"{ym}-01"
        last_day_num = _cal.monthrange(yy, mm)[1]
        last_day = f"{ym}-{last_day_num:02d}"
        # Paginate: a busy month (30 days × 4 markets × many ASINs) easily
        # exceeds Supabase's default 1000-row response cap.
        page = 0
        page_size = 1000
        rows: list[dict] = []
        while True:
            try:
                chunk = (
                    db_admin.table("sales_velocity")
                    .select("asin,marketplace,units_sold_30d,period_end_date")
                    .gte("period_end_date", first_day)
                    .lte("period_end_date", last_day)
                    .order("period_end_date", desc=True)
                    .range(page * page_size, (page + 1) * page_size - 1)
                    .execute().data or []
                )
            except Exception as exc:
                logger.warning("forecast_engine  %s page %d failed: %s", ym, page, exc)
                break
            rows.extend(chunk)
            if len(chunk) < page_size:
                break
            page += 1
            if page > 20:   # hard cap to prevent runaway (20k rows per month would be nuts)
                break
        # Within this month, keep the LATEST snapshot per (asin, mp) — but
        # DO NOT overwrite values we already got from sales_monthly (preferred).
        rows.sort(key=lambda r: r.get("period_end_date") or "", reverse=True)
        seen_in_month: set[tuple] = set()
        for r in rows:
            asin = r["asin"]
            mp   = r["marketplace"]
            if mp not in MARKETS:
                continue
            key = (asin, mp)
            if key in seen_in_month:
                continue
            seen_in_month.add(key)
            if ym in history.get(key, {}):
                continue   # sales_monthly already has this one
            history[key][ym] = int(r.get("units_sold_30d") or 0)
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
    """Regenerate projections AND cache historical aggregates in
    forecast_computed. After this runs, the dashboard can read everything
    (history + projections) from a single table instead of re-aggregating
    sales_velocity on every page load.

    Writes rows with:
        source='history'  for aggregated past months
        source='auto'     for projected future months
    Does NOT overwrite source='manual_override' rows (user edits are kept).

    If `asin` is given, only that product; otherwise all products.
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

    # Protect user overrides across both history and future projections
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
        # Resolve growth: per-country override → product-wide → auto-suggest → default
        growth = _resolve_growth(settings, a, mp)
        if (a, mp) not in settings and (a, "ALL") not in settings:
            # No manual setting at any level — try the data-driven suggestion
            suggested = _suggest_growth_from_history(months)
            if suggested is not None:
                growth = suggested

        # 1) Cache historical months in forecast_computed with source='history'
        for ym, units in months.items():
            if (a, mp, ym) in overrides:
                continue
            to_upsert.append({
                "asin":          a,
                "marketplace":   mp,
                "year_month":    ym,
                "units":         int(units),
                "growth_pct":    round(growth, 2),
                "baseline_avg":  None,
                "source":        "history",
                "updated_at":    now_iso,
            })

        # 2) Project future months with source='auto'
        if len(months) < MIN_HISTORY:
            warnings.append(f"{a} {mp}: only {len(months)} month(s) history, skipped projection")
            continue
        projections = _project(months, growth)
        if not projections:
            warnings.append(f"{a} {mp}: baseline is zero, skipped projection")
            continue
        for ym, units, baseline in projections:
            if (a, mp, ym) in overrides:
                continue
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
    """Fast read for the dashboard — pulls everything (history + projections)
    from forecast_computed in a single query. This REPLACES the old flow of
    re-aggregating sales_velocity on every page load.

    Returns dict[(asin, mp)] → {history, projections, growth_pct}.
    """
    settings = _load_growth_settings()

    try:
        q = db_admin.table("forecast_computed").select("*")
        if asin:
            q = q.eq("asin", asin)
        # Paginate to be safe (12 months × 15 ASINs × 5 markets = 900 rows,
        # but it will grow as we add more products).
        rows: list[dict] = []
        page = 0
        page_size = 1000
        while True:
            chunk = q.range(page * page_size, (page + 1) * page_size - 1).execute().data or []
            rows.extend(chunk)
            if len(chunk) < page_size:
                break
            page += 1
            if page > 10:
                break
    except Exception as exc:
        logger.warning("forecast_engine  load failed: %s", exc)
        rows = []

    hist_by_key: dict[tuple, dict[str, int]] = defaultdict(dict)
    proj_by_key: dict[tuple, dict[str, dict]] = defaultdict(dict)
    asins_seen: set[str] = set()
    for r in rows:
        a  = r["asin"]
        mp = r["marketplace"]
        ym = r["year_month"]
        src = r.get("source", "auto")
        asins_seen.add(a)
        if src == "history":
            hist_by_key[(a, mp)][ym] = int(r.get("units") or 0)
        else:
            # 'auto' or 'manual_override' both count as projections
            proj_by_key[(a, mp)][ym] = r

    out: dict[tuple, dict] = {}
    all_keys = set(hist_by_key.keys()) | set(proj_by_key.keys())
    for key in all_keys:
        a, mp = key
        hist = dict(sorted(hist_by_key.get(key, {}).items()))
        growth_manual = settings.get((a, mp))   # per-country override
        growth_prod   = settings.get((a, "ALL"))  # product-wide override
        growth_suggested = _suggest_growth_from_history(hist)
        if growth_manual is not None:
            growth_used = growth_manual
            growth_src = "manual_country"
        elif growth_prod is not None:
            growth_used = growth_prod
            growth_src = "manual_product"
        elif growth_suggested is not None:
            growth_used = growth_suggested
            growth_src = "auto"
        else:
            growth_used = DEFAULT_GROW
            growth_src = "default"
        out[key] = {
            "history":          hist,
            "projections":      proj_by_key.get(key, {}),
            "growth_pct":       growth_used,
            "growth_source":    growth_src,
            "growth_suggested": growth_suggested,
            "growth_manual":    growth_manual,
            "growth_product":   growth_prod,
        }
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    n, warns = regenerate_forecast()
    print(f"Wrote {n} rows")
    for w in warns[:10]:
        print(" ⚠", w)
