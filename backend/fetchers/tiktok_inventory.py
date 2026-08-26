"""Fetch FBT (Fulfilled by TikTok) inventory from TikTok Shop API.

Auth model
----------
TikTok Shop uses a signed-request scheme:
  1. Get an access_token via OAuth (one-time, stored in secrets)
  2. For each API call, build query params + a HMAC-SHA256 signature
     using the App Secret
  3. Send request with access_token header

Endpoint
--------
POST /fulfillment/202309/fbt/inventory/search
Docs: https://partner.tiktokshop.com/docv2/page/650a8fa4390d0f02b498d1c8

Env vars required (Streamlit Cloud Secrets)
-------------------------------------------
TIKTOK_APP_KEY          — App Key from Partner Center
TIKTOK_APP_SECRET       — App Secret (keep private)
TIKTOK_ACCESS_TOKEN     — Obtained via OAuth once, refresh every 30 days
TIKTOK_SHOP_CIPHER      — Shop cipher (from auth response, per-seller)

Behaviour
---------
- Pulls current FBT stock per SKU per warehouse
- Aggregates to per-ASIN totals (matches our internal keying)
- Upserts into `tiktok_stock` table so the dashboard sees fresh numbers
- Returns (num_asins_updated, warnings) so caller can log
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone

import httpx

from backend.database import db_admin

logger = logging.getLogger(__name__)

_BASE_URL = "https://open-api.tiktokglobalshop.com"
_ENDPOINT = "/fulfillment/202309/fbt/inventory/search"


# ── Signing helpers ──────────────────────────────────────────────

def _sign_request(app_secret: str, path: str, params: dict[str, str]) -> str:
    """Compute the HMAC-SHA256 signature TikTok expects on every request.

    Formula: HMAC-SHA256(app_secret,
        app_secret + path + concat(k+v for sorted params EXCLUDING 'sign' and 'access_token') + app_secret
    ).hexdigest()
    """
    # Sort params alphabetically, exclude sign & access_token
    filtered = {k: v for k, v in params.items() if k not in ("sign", "access_token")}
    concat   = "".join(f"{k}{v}" for k, v in sorted(filtered.items()))
    payload  = f"{app_secret}{path}{concat}{app_secret}"
    return hmac.new(
        app_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _build_signed_params(app_key: str, app_secret: str, access_token: str,
                        shop_cipher: str, extra: dict | None = None) -> dict:
    """Base query params every request needs, with signature."""
    now = int(time.time())
    params = {
        "app_key":   app_key,
        "timestamp": str(now),
        "shop_cipher": shop_cipher,
        "version":   "202309",
    }
    if extra:
        params.update(extra)
    params["sign"]         = _sign_request(app_secret, _ENDPOINT, params)
    params["access_token"] = access_token
    return params


# ── FBT inventory fetch ──────────────────────────────────────────

def fetch_fbt_inventory() -> tuple[int, list[str]]:
    """Query TikTok Shop for current FBT inventory and upsert into tiktok_stock.

    Returns (num_asins_updated, warnings). Missing creds → returns (0, [warning]).
    Any API/network error is caught and returned as a warning so the caller
    can decide how to surface it (dashboard alert vs email log).
    """
    app_key      = os.getenv("TIKTOK_APP_KEY", "").strip()
    app_secret   = os.getenv("TIKTOK_APP_SECRET", "").strip()
    access_token = os.getenv("TIKTOK_ACCESS_TOKEN", "").strip()
    shop_cipher  = os.getenv("TIKTOK_SHOP_CIPHER", "").strip()

    if not all([app_key, app_secret, access_token, shop_cipher]):
        return 0, ["TikTok credentials not configured (need TIKTOK_APP_KEY, "
                   "TIKTOK_APP_SECRET, TIKTOK_ACCESS_TOKEN, TIKTOK_SHOP_CIPHER)"]

    warnings: list[str] = []
    all_items: list[dict] = []
    page_token = ""
    max_pages  = 20  # safety cap — 20 × 100 = 2000 SKUs, way more than we'd ever have

    for page_i in range(max_pages):
        body = {"page_size": 100}
        if page_token:
            body["page_token"] = page_token

        params = _build_signed_params(app_key, app_secret, access_token, shop_cipher)
        try:
            r = httpx.post(
                _BASE_URL + _ENDPOINT,
                params=params,
                json=body,
                headers={
                    "x-tts-access-token": access_token,
                    "content-type":       "application/json",
                },
                timeout=15,
            )
        except Exception as exc:
            warnings.append(f"HTTP error on page {page_i}: {exc}")
            break

        if r.status_code != 200:
            warnings.append(f"HTTP {r.status_code} on page {page_i}: {r.text[:200]}")
            break

        payload = r.json()
        if payload.get("code") != 0:
            warnings.append(f"API error {payload.get('code')}: {payload.get('message')}")
            break

        items = (payload.get("data", {}) or {}).get("inventory_list", []) or []
        all_items.extend(items)

        page_token = (payload.get("data", {}) or {}).get("next_page_token", "")
        if not page_token:
            break

    if not all_items:
        return 0, (warnings or ["No FBT inventory items returned"])

    # Aggregate per-SKU rows into per-ASIN totals. Each item typically has:
    #   { "seller_sku": "...", "product_id": "...", "sku_id": "...",
    #     "available_quantity": 123, "warehouse_id": "..." }
    # We don't have a direct ASIN mapping (TikTok uses its own IDs) — fall
    # back to seller_sku as the join key against products.sku.
    per_sku: dict[str, int] = {}
    for it in all_items:
        sku = (it.get("seller_sku") or "").strip()
        if not sku:
            continue
        qty = int(it.get("available_quantity") or 0)
        per_sku[sku] = per_sku.get(sku, 0) + qty

    # Look up ASIN by SKU
    sku_rows = db_admin.table("products").select("asin,sku").execute().data or []
    sku_to_asin = {r["sku"]: r["asin"] for r in sku_rows if r.get("sku")}

    now_iso = datetime.now(timezone.utc).isoformat()
    updated = 0
    unknown_skus: list[str] = []
    for sku, qty in per_sku.items():
        asin = sku_to_asin.get(sku)
        if not asin:
            unknown_skus.append(sku)
            continue
        try:
            db_admin.table("tiktok_stock").upsert(
                {
                    "asin":       asin,
                    "units":      qty,
                    "updated_at": now_iso,
                    "notes":      "Auto-synced from TikTok Shop API",
                },
                on_conflict="asin",
            ).execute()
            updated += 1
        except Exception as exc:
            warnings.append(f"Failed to save {asin}: {exc}")

    if unknown_skus:
        warnings.append(f"{len(unknown_skus)} SKU(s) from TikTok not in products table: "
                        f"{', '.join(unknown_skus[:5])}"
                        + ("…" if len(unknown_skus) > 5 else ""))

    logger.info("tiktok  %d ASINs updated (%d warnings)", updated, len(warnings))
    return updated, warnings
