"""Sync product metadata using the Listings Items API (v2021-08-01)."""

import logging
import os
import time

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from backend.config import SP_API_CREDENTIALS, MARKETPLACES
from backend.database import db_admin, upsert, log_sync

logger = logging.getLogger(__name__)

_PRIMARY_MP_ID = MARKETPLACES["ES"]   # ES marketplace ID — Spanish product names
_ENDPOINT      = "https://sellingpartnerapi-eu.amazon.com"
_SELLER_ID     = os.getenv("SELLER_ID", "")


def _get_access_token() -> str:
    resp = httpx.post("https://api.amazon.com/auth/o2/token", data={
        "grant_type":    "refresh_token",
        "refresh_token": SP_API_CREDENTIALS["refresh_token"],
        "client_id":     SP_API_CREDENTIALS["lwa_app_id"],
        "client_secret": SP_API_CREDENTIALS["lwa_client_secret"],
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def _get_known_products() -> list[dict]:
    """Return list of {asin, sku} from inventory_snapshots joined with products."""
    inv_rows = db_admin.table("inventory_snapshots").select("asin").execute().data or []
    asins = sorted({r["asin"] for r in inv_rows})
    prod_rows = db_admin.table("products").select("asin,sku").execute().data or []
    sku_map = {r["asin"]: r.get("sku", "") for r in prod_rows}
    return [{"asin": a, "sku": sku_map.get(a, "")} for a in asins]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
       retry=retry_if_exception_type(Exception), reraise=False)
def _fetch_name_by_sku(access_token: str, sku: str) -> str | None:
    """Get product name via Listings Items API (works with existing roles)."""
    if not sku or not _SELLER_ID:
        return None
    r = httpx.get(
        f"{_ENDPOINT}/listings/2021-08-01/items/{_SELLER_ID}/{sku}",
        params={"marketplaceIds": _PRIMARY_MP_ID, "includedData": "summaries"},
        headers={"x-amz-access-token": access_token, "content-type": "application/json"},
        timeout=10,
    )
    if r.status_code == 200:
        summaries = r.json().get("summaries", [])
        return summaries[0].get("itemName") if summaries else None
    return None


def fetch_catalog() -> int:
    products = _get_known_products()
    if not products:
        logger.info("catalog  no ASINs in inventory_snapshots yet, skipping")
        return 0

    try:
        access_token = _get_access_token()
    except Exception as exc:
        logger.error("catalog  failed to get access token: %s", exc)
        log_sync("catalog", None, "error", 0, str(exc))
        return 0

    rows: list[dict] = []
    for p in products:
        asin = p["asin"]
        sku  = p.get("sku", "")
        name = None

        if sku:
            try:
                name = _fetch_name_by_sku(access_token, sku)
                time.sleep(0.3)
            except Exception as exc:
                logger.warning("catalog  %s  listings API failed: %s", asin, exc)

        if name:
            rows.append({"asin": asin, "product_name": name, "sku": sku})
            logger.debug("catalog  %s  → %s", asin, name[:50])
        else:
            logger.warning("catalog  %s  no name found, keeping existing", asin)

    count = upsert("products", rows, "asin") if rows else 0
    logger.info("catalog  → %d products synced", count)
    log_sync("catalog", None, "success", count)
    return count
