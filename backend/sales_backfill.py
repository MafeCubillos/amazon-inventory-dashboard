"""One-shot backfill: pull historical monthly sales from Amazon SP-API and
insert them into sales_velocity so the forecast engine has 12+ months of
history to work with.

RATE LIMITS (Amazon SP-API Reports): 0.0167 req/sec (= 1 per 60 sec), burst 15.
We sleep 65s between report requests to stay well within limits. Reports that
succeeded earlier are preserved (upsert is idempotent), so you can re-run.

Uses the same GET_SALES_AND_TRAFFIC_REPORT as backend/fetchers/sales.py but
loops through past months instead of pulling only the trailing 30 days.

For each (marketplace, month):
    1. Create a report for that month's window
    2. Poll until DONE
    3. Download + parse
    4. Insert one synthetic snapshot into sales_velocity with:
         - period_end_date = last day of the month
         - units_sold_30d  = the month's total units
         - velocity_daily  = month_units / days_in_month
         - source          = 'backfill_spapi' (so it's identifiable)

Idempotent: upserts on (asin, marketplace, period_end_date), so re-runs
don't double-count.

Note on timing: each SP-API report takes 2-5 min to process. For 12 months
× 4 markets = 48 reports × ~3 min = ~2.5 hours total. Reports run serially
to stay within rate limits. The function yields progress updates so the
Streamlit UI can show a progress bar.
"""

from __future__ import annotations

import calendar
import logging
import time
from datetime import date, timedelta
from typing import Iterator

# Amazon SP-API Reports endpoint allows 1 create per 60s (0.0167 req/s).
# In practice the burst quota is tighter than docs suggest, so we sleep 90s
# to avoid repeated 429 QuotaExceeded errors.
_SLEEP_BETWEEN_REPORTS_SEC = 90

from sp_api.api import Reports
from sp_api.base import Marketplaces

from backend.config import SP_API_CREDENTIALS
from backend.database import db_admin, upsert
from backend.fetchers.sales import (
    _create_report, _poll_report, _download_report, _parse_and_aggregate,
    _MARKETPLACE_ENUM,
)

logger = logging.getLogger(__name__)

MARKETS = ["ES", "FR", "DE", "IT"]


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    """Return (first_day, last_day) of the given month."""
    first = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    return first, date(year, month, last_day)


def _past_months(n: int = 12) -> list[tuple[int, int]]:
    """Return list of (year, month) tuples for the last N complete months,
    oldest first. Excludes the current month."""
    today = date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
        out.append((y, m))
    return list(reversed(out))


def backfill_one_month(marketplace: str, year: int, month: int) -> tuple[int, str]:
    """Pull one month of sales for one marketplace and upsert to sales_velocity.
    Returns (rows_written, status_msg).
    """
    start, end = _month_bounds(year, month)
    period_end_label = end.isoformat()
    mp_enum = _MARKETPLACE_ENUM[marketplace]
    api = Reports(credentials=SP_API_CREDENTIALS, marketplace=mp_enum)

    logger.info("backfill  %s  %04d-%02d  creating report (%s → %s)",
                marketplace, year, month, start, end)
    try:
        report_id = _create_report(api, start, end)
        doc_id    = _poll_report(api, report_id)
        tsv_text  = _download_report(api, doc_id)
    except Exception as exc:
        return 0, f"❌ {marketplace} {year}-{month:02d}: {exc}"

    df = _parse_and_aggregate(tsv_text, end)
    if df.empty:
        return 0, f"⚠️ {marketplace} {year}-{month:02d}: 0 rows returned"

    # Only ASINs known in products table (FK)
    known = {r["asin"] for r in db_admin.table("products").select("asin").execute().data or []}
    days_in_month = (end - start).days + 1

    rows = []
    for _, r in df.iterrows():
        asin = r["asin"]
        if asin not in known:
            continue
        units_month = int(r["units_sold_30d"])  # the report sums the whole window
        rows.append({
            "asin":            asin,
            "marketplace":     marketplace,
            "units_sold_7d":   int(round(units_month * 7 / days_in_month)),
            "units_sold_14d":  int(round(units_month * 14 / days_in_month)),
            "units_sold_30d":  units_month,
            "velocity_daily":  round(units_month / days_in_month, 4),
            "period_end_date": period_end_label,
        })

    if not rows:
        return 0, f"⚠️ {marketplace} {year}-{month:02d}: no matching ASINs"

    try:
        count = upsert("sales_velocity", rows, "asin,marketplace,period_end_date")
    except Exception as exc:
        return 0, f"❌ {marketplace} {year}-{month:02d}: upsert failed: {exc}"

    return count, f"✅ {marketplace} {year}-{month:02d}: {count} rows"


def backfill_all(months: int = 12) -> Iterator[dict]:
    """Generator that yields progress dicts after each (market, month).
    Caller (e.g. Streamlit) can use these for progress bars.

    Yields: {marketplace, year, month, written, msg, done, total}
    """
    period_list = _past_months(months)
    total = len(period_list) * len(MARKETS)

    # Pre-check which (marketplace, exact-last-day-of-month) rows we already
    # backfilled, so we skip them on reruns. Query each want_date individually
    # (small, fast, dodges the default 1000-row cap, and we only have ~12).
    want_dates = sorted({
        _month_bounds(y, m)[1].isoformat() for (y, m) in period_list
    })
    existing: set[tuple[str, str]] = set()
    for d in want_dates:
        try:
            rows = (
                db_admin.table("sales_velocity")
                .select("marketplace")
                .eq("period_end_date", d)
                .execute().data or []
            )
            # If any row exists for this exact date, mark that (mp, date)
            # as already present.
            for r in rows:
                existing.add((r["marketplace"], d))
        except Exception:
            continue

    done = 0
    for mp in MARKETS:
        for (y, m) in period_list:
            done += 1
            ym = f"{y:04d}-{m:02d}"
            _, last = _month_bounds(y, m)
            last_iso = last.isoformat()
            if (mp, last_iso) in existing:
                yield {
                    "marketplace": mp, "year": y, "month": m,
                    "written": 0,
                    "msg": f"⏭️  {mp} {ym}: already in DB, skipped",
                    "done": done, "total": total,
                }
                continue
            n, msg = backfill_one_month(mp, y, m)
            yield {
                "marketplace": mp, "year": y, "month": m,
                "written": n, "msg": msg, "done": done, "total": total,
            }
            # Respect SP-API Reports quota (1 create / 60s). Skip the sleep
            # after the very last iteration so we don't dawdle on exit.
            if done < total:
                time.sleep(_SLEEP_BETWEEN_REPORTS_SEC)


if __name__ == "__main__":
    import sys
    # Auto-load .env so credentials are present when run as a script.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO)
    months = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    total_written = 0
    for update in backfill_all(months):
        print(f"[{update['done']}/{update['total']}] {update['msg']}", flush=True)
        total_written += update["written"]
    print(f"\n🎉 Backfill complete: {total_written} rows across "
          f"{months} months × {len(MARKETS)} markets")
