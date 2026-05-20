"""Fetch FBA inventory using GET_AFN_INVENTORY_DATA_BY_COUNTRY report.

This report gives accurate per-country physical inventory (quantity-for-local-fulfillment)
which matches Seller Central's per-marketplace view. The FBA Inventory API returns EU-total
fulfillable quantity which is not per-country — this report is the correct data source.
"""

import csv
import io
import logging
import time
from datetime import date

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from backend.config import SP_API_CREDENTIALS, MARKETPLACES
from backend.database import upsert, log_sync

logger = logging.getLogger(__name__)

_ENDPOINT   = "https://sellingpartnerapi-eu.amazon.com"
_REPORT_TYPE = "GET_AFN_INVENTORY_DATA_BY_COUNTRY"

# Map country code in report → our marketplace codes
_COUNTRY_TO_MP = {
    "ES": "ES", "FR": "FR", "DE": "DE", "IT": "IT",
    "PL": None, "NL": None, "SE": None, "BE": None,
    "GB": None, "IE": None, "AE": None, "SA": None,
}


def _get_access_token() -> str:
    resp = httpx.post("https://api.amazon.com/auth/o2/token", data={
        "grant_type":    "refresh_token",
        "refresh_token": SP_API_CREDENTIALS["refresh_token"],
        "client_id":     SP_API_CREDENTIALS["lwa_app_id"],
        "client_secret": SP_API_CREDENTIALS["lwa_client_secret"],
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _request_report(access_token: str) -> str:
    # Try each EU marketplace until one succeeds (FATAL can happen if no inventory in that MP)
    for mp_code in ["ES", "DE", "FR", "IT"]:
        r = httpx.post(
            f"{_ENDPOINT}/reports/2021-06-30/reports",
            json={"reportType": _REPORT_TYPE, "marketplaceIds": [MARKETPLACES[mp_code]]},
            headers={"x-amz-access-token": access_token, "content-type": "application/json"},
            timeout=15,
        )
        if r.status_code in (200, 202):
            return r.json()["reportId"]
    r.raise_for_status()
    return r.json()["reportId"]


def _poll_report(access_token: str, report_id: str, max_wait: int = 300) -> str:
    """Poll until DONE, return reportDocumentId."""
    for _ in range(max_wait // 10):
        time.sleep(10)
        r = httpx.get(
            f"{_ENDPOINT}/reports/2021-06-30/reports/{report_id}",
            headers={"x-amz-access-token": access_token},
            timeout=15,
        )
        status = r.json().get("processingStatus")
        if status == "DONE":
            return r.json()["reportDocumentId"]
        if status == "FATAL":
            raise RuntimeError(f"Report {report_id} FATAL: {r.json()}")
        logger.debug("inventory report %s status: %s", report_id, status)
    raise TimeoutError(f"Report {report_id} did not complete in {max_wait}s")


def _download_report(access_token: str, doc_id: str) -> str:
    r = httpx.get(
        f"{_ENDPOINT}/reports/2021-06-30/documents/{doc_id}",
        headers={"x-amz-access-token": access_token},
        timeout=15,
    )
    r.raise_for_status()
    r2 = httpx.get(r.json()["url"], timeout=30)
    r2.raise_for_status()
    return r2.text


def _parse_report(text: str, today: str) -> list[dict]:
    """Parse TSV into inventory rows per (asin, marketplace).
    Explicitly writes 0 for all 4 marketplaces even if not in report,
    so old EU-total data is overwritten correctly.
    """
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")

    # Use MAX per (asin, country) — multiple SKUs share physical stock
    raw: dict[tuple, int] = {}
    all_asins: set[str] = set()

    for row in reader:
        asin    = row.get("asin", "").strip()
        country = row.get("country", "").strip().upper()
        mp      = _COUNTRY_TO_MP.get(country)
        if not asin or mp is None:
            continue
        qty = int(row.get("quantity-for-local-fulfillment", 0) or 0)
        raw[(asin, mp)] = max(raw.get((asin, mp), 0), qty)
        all_asins.add(asin)

    # Build ALL asin × marketplace combinations (0 for missing = no stock there)
    eu_mps = [mp for mp, v in _COUNTRY_TO_MP.items() if v is not None]
    rows = []
    for asin in all_asins:
        for mp in eu_mps:
            rows.append({
                "asin":            asin,
                "marketplace":     mp,
                "units_available": raw.get((asin, mp), 0),
                "units_reserved":  0,
                "units_inbound":   0,
                "stock_value_eur": 0,
                "snapshot_date":   today,
            })
    return rows


def fetch_all_inventory() -> dict[str, int]:
    today = date.today().isoformat()
    try:
        access_token = _get_access_token()
        report_id    = _request_report(access_token)
        logger.info("inventory  report requested: %s", report_id)
        doc_id       = _poll_report(access_token, report_id)
        text         = _download_report(access_token, doc_id)
        rows         = _parse_report(text, today)
    except Exception as exc:
        logger.error("inventory  report failed: %s", exc)
        for code in MARKETPLACES:
            log_sync("inventory", code, "error", 0, str(exc))
        return {code: 0 for code in MARKETPLACES}

    # Also fetch inbound from FBA Inventory API
    rows = _add_inbound(access_token, rows, today)

    count = upsert("inventory_snapshots", rows, "asin,marketplace,snapshot_date")
    logger.info("inventory  → %d total records across all marketplaces", count)

    # Log per marketplace
    results: dict[str, int] = {}
    for code in MARKETPLACES:
        mp_count = sum(1 for r in rows if r["marketplace"] == code)
        log_sync("inventory", code, "success", mp_count)
        results[code] = mp_count
    return results


def _add_inbound(access_token: str, rows: list[dict], today: str) -> list[dict]:
    """Add inbound quantities from FBA Inventory API (these are correct per-marketplace)."""
    from sp_api.api import Inventories
    from sp_api.base import Marketplaces as MP
    from backend.config import SP_API_CREDENTIALS

    _MP_ENUM = {"ES": MP.ES, "FR": MP.FR, "DE": MP.DE, "IT": MP.IT}
    inbound_map: dict[tuple, int] = {}

    for code, mp_id in MARKETPLACES.items():
        try:
            api = Inventories(credentials=SP_API_CREDENTIALS, marketplace=_MP_ENUM[code])
            payload = api.get_inventory_summary_marketplace(
                details=True, granularityType="Marketplace", granularityId=mp_id
            ).payload
            seen: dict[str, int] = {}
            for s in payload.get("inventorySummaries", []):
                asin = s.get("asin")
                if not asin:
                    continue
                inv = s.get("inventoryDetails", {})
                inb = int(
                    (inv.get("inboundWorkingQuantity") or 0)
                    + (inv.get("inboundShippedQuantity") or 0)
                    + (inv.get("inboundReceivingQuantity") or 0)
                )
                seen[asin] = max(seen.get(asin, 0), inb)
            for asin, inb in seen.items():
                inbound_map[(asin, code)] = inb
        except Exception as exc:
            logger.warning("inventory inbound %s failed: %s", code, exc)

    # Merge inbound into rows
    for r in rows:
        key = (r["asin"], r["marketplace"])
        r["units_inbound"] = inbound_map.get(key, 0)
    return rows


# Keep backwards compat
def fetch_inventory_for_marketplace(marketplace_code: str) -> int:
    results = fetch_all_inventory()
    return results.get(marketplace_code, 0)
