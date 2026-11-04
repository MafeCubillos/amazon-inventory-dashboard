"""Scan reorder alerts, auto-create draft POs, and email María a summary.

Invoked by the GitHub Actions workflow .github/workflows/po_auto_scan.yml
on Mon + Thu at 09:00 Spain time.
"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("po_auto_scan")


def main() -> int:
    from backend.po_auto_draft import scan_and_create_drafts, send_draft_alert_email

    log.info("=== scanning reorder alerts for auto-drafts ===")
    try:
        drafts = scan_and_create_drafts(threshold_pct=0.70)
    except Exception as exc:
        log.exception("scan failed: %s", exc)
        return 1

    log.info("scan produced %d new draft(s)", len(drafts))
    for d in drafts:
        log.info("  · PO %s  %s  qty=%s",
                 d["po"].get("po_number"),
                 d["product"].get("name"),
                 d["final_qty"])

    if not drafts:
        log.info("nothing to email — exiting clean.")
        return 0

    log.info("=== emailing summary ===")
    ok, msg = send_draft_alert_email(drafts)
    log.info("email result: ok=%s msg=%s", ok, msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
