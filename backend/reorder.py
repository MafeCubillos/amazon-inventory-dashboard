"""Reorder alert calculation.

For each (asin, marketplace) pair that has both an inventory snapshot and a
sales velocity record, this module computes:

  days_of_stock_left  = units_available / velocity_daily
  days_until_reorder  = days_of_stock_left - lead_time_days
  alert_status        = critical | warning | ok
  suggested_reorder_qty = max(0, (target_days_coverage - days_of_stock_left) * velocity_daily)
  stock_value_eur     = units_available * cogs_eur  (written back to inventory_snapshots)

Results are upserted into reorder_alerts (one row per asin+marketplace).
"""

import logging
from datetime import date

from backend.database import db_admin, upsert, log_sync

logger = logging.getLogger(__name__)

_MARKETPLACES = ("ES", "FR", "DE", "IT")


def _fetch_latest_inventory() -> list[dict]:
    """Return one row per (asin, marketplace) — the most recent snapshot."""
    try:
        resp = db_admin.rpc("latest_inventory", {}).execute()
        if resp.data:
            return resp.data
    except Exception:
        pass

    # Fallback: Python-side deduplication if the RPC view doesn't exist yet
    rows = db_admin.table("inventory_snapshots").select("*").execute().data or []
    seen: dict[tuple, dict] = {}
    for r in sorted(rows, key=lambda x: x["snapshot_date"], reverse=True):
        key = (r["asin"], r["marketplace"])
        if key not in seen:
            seen[key] = r
    return list(seen.values())


def _fetch_latest_velocity() -> list[dict]:
    try:
        resp = db_admin.rpc("latest_velocity", {}).execute()
        if resp.data:
            return resp.data
    except Exception:
        pass

    rows = db_admin.table("sales_velocity").select("*").execute().data or []
    seen: dict[tuple, dict] = {}
    for r in sorted(rows, key=lambda x: x["period_end_date"], reverse=True):
        key = (r["asin"], r["marketplace"])
        if key not in seen:
            seen[key] = r
    return list(seen.values())


def _fetch_products() -> dict[str, dict]:
    rows = db_admin.table("products").select("asin,cogs_eur,lead_time_days,target_days_coverage").execute().data or []
    return {r["asin"]: r for r in rows}


def calculate_reorder_alerts() -> int:
    inventory = _fetch_latest_inventory()
    velocity  = _fetch_latest_velocity()
    products  = _fetch_products()

    # Index velocity by (asin, marketplace)
    vel_index: dict[tuple, dict] = {
        (r["asin"], r["marketplace"]): r for r in velocity
    }

    alert_rows:     list[dict] = []
    inv_value_updates: list[dict] = []

    for inv in inventory:
        asin       = inv["asin"]
        marketplace = inv["marketplace"]
        key        = (asin, marketplace)

        vel = vel_index.get(key)
        prd = products.get(asin, {})

        velocity_daily     = float((vel or {}).get("velocity_daily", 0) or 0)
        units_available    = int(inv.get("units_available", 0) or 0)
        lead_time_days     = int(prd.get("lead_time_days", 30) or 30)
        target_coverage    = int(prd.get("target_days_coverage", 60) or 60)
        cogs_eur           = float(prd.get("cogs_eur", 0) or 0)

        # Guard against zero velocity (product is not selling yet)
        if velocity_daily > 0:
            days_of_stock_left = round(units_available / velocity_daily, 2)
        else:
            days_of_stock_left = 9999.0  # infinite stock — no sales data

        days_until_reorder = round(days_of_stock_left - lead_time_days, 2)

        if days_of_stock_left < lead_time_days:
            alert_status = "critical"
        elif days_of_stock_left < (lead_time_days + 15):
            alert_status = "warning"
        else:
            alert_status = "ok"

        if velocity_daily > 0 and days_of_stock_left < target_coverage:
            suggested_qty = max(0, round((target_coverage - days_of_stock_left) * velocity_daily))
        else:
            suggested_qty = 0

        alert_rows.append({
            "asin":                  asin,
            "marketplace":           marketplace,
            "days_of_stock_left":    days_of_stock_left,
            "days_until_reorder":    days_until_reorder,
            "alert_status":          alert_status,
            "suggested_reorder_qty": int(suggested_qty),
            "calculated_at":         date.today().isoformat(),
        })

        # Enrich inventory snapshot with stock value using COGS
        if cogs_eur > 0 and inv.get("id"):
            inv_value_updates.append({
                "id":              inv["id"],
                "stock_value_eur": round(units_available * cogs_eur, 2),
            })

    count = upsert("reorder_alerts", alert_rows, "asin,marketplace")

    # Write stock values back into inventory_snapshots
    for upd in inv_value_updates:
        db_admin.table("inventory_snapshots").update(
            {"stock_value_eur": upd["stock_value_eur"]}
        ).eq("id", upd["id"]).execute()

    logger.info("reorder  → %d alerts calculated", count)
    log_sync("reorder", None, "success", count)
    return count
