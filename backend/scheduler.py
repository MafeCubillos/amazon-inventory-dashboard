"""APScheduler-based sync runner.

Run this process alongside the Streamlit dashboard:

    python -m backend.scheduler

The scheduler fires every SYNC_INTERVAL_HOURS hours (default 6).
A full sync pipeline runs in this order:
  1. catalog  — ensure products table is populated
  2. inventory — FBA stock levels per marketplace
  3. sales     — 30-day sales velocity per marketplace
  4. reorder   — recalculate alerts from the fresh data
"""

import logging
import os
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from backend.config import SYNC_INTERVAL_HOURS
from backend.fetchers.inventory import fetch_all_inventory
from backend.fetchers.sales import fetch_all_sales
from backend.fetchers.catalog import fetch_catalog
from backend.reorder import calculate_reorder_alerts

logger = logging.getLogger(__name__)


def run_full_sync() -> None:
    logger.info("══════════ SYNC START ══════════")
    try:
        fetch_catalog()
    except Exception as exc:
        logger.error("catalog sync failed: %s", exc)

    try:
        fetch_all_inventory()
    except Exception as exc:
        logger.error("inventory sync failed: %s", exc)

    try:
        fetch_all_sales()
    except Exception as exc:
        logger.error("sales sync failed: %s", exc)

    try:
        calculate_reorder_alerts()
    except Exception as exc:
        logger.error("reorder calculation failed: %s", exc)

    logger.info("══════════ SYNC END   ══════════")


def send_alerts() -> None:
    """Send inventory alert email (Mon & Thu). Only runs if email is configured."""
    try:
        from backend.alerts import send_alert_email
        send_alert_email()
    except Exception as exc:
        logger.error("alert email failed: %s", exc)


def main() -> None:
    # Run once immediately on start so data is fresh
    run_full_sync()

    alert_hour = int(os.getenv("ALERT_EMAIL_HOUR", "8"))

    scheduler = BlockingScheduler(timezone="UTC")

    # ── Data sync every N hours ────────────────────────────────
    scheduler.add_job(
        run_full_sync,
        trigger=IntervalTrigger(hours=SYNC_INTERVAL_HOURS),
        id="full_sync",
        name=f"Full SP-API sync every {SYNC_INTERVAL_HOURS}h",
        replace_existing=True,
    )

    # ── Alert email every Monday and Thursday at 8:00 AM UTC ──
    scheduler.add_job(
        send_alerts,
        trigger=CronTrigger(day_of_week="mon,thu", hour=alert_hour, minute=0, timezone="UTC"),
        id="alert_email",
        name=f"Inventory alert email (Mon & Thu at {alert_hour:02d}:00 UTC)",
        replace_existing=True,
    )

    logger.info(
        "Scheduler started — sync every %dh, alerts Mon & Thu at %02d:00 UTC. "
        "Press Ctrl+C to stop.",
        SYNC_INTERVAL_HOURS, alert_hour,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
