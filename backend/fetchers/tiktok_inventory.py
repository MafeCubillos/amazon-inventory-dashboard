"""Fetch TikTok Shop inventory using the Product Search API.

Why not FBT inventory endpoint?
-------------------------------
TikTok exposes an FBT (Fulfilled by TikTok) inventory search under
`/fulfillment/202309/fbt/inventory/search`, but that path returns 404
"Invalid path" for many Custom apps / regions — it seems to require an
extra qualification the OMS/WMS category doesn't include.

The Product Search API (`/product/202309/products/search`) is available
to any app with the `seller.product.basic` scope, works across all
regions, and returns per-SKU inventory across every warehouse the shop
uses (including FBT warehouses). That's the union we actually want.

Auth model
----------
Each request needs:
  - Query params signed with HMAC-SHA256(app_secret, ...)
  - `x-tts-access-token` header carrying the OAuth token

Env vars required (Streamlit Cloud Secrets)
-------------------------------------------
TIKTOK_APP_KEY          — App Key from Partner Center
TIKTOK_APP_SECRET       — App Secret (keep private)
TIKTOK_ACCESS_TOKEN     — Obtained via OAuth, refresh every 30 days
TIKTOK_SHOP_CIPHER      — Shop cipher (from /authorization/202309/shops)

Behaviour
---------
- Pulls every active product + all its SKUs (paginated)
- Aggregates each SKU's stock across warehouses
- Joins to our `products` table via SKU (TikTok "seller_sku" == Amazon SKU)
- Upserts into `tiktok_stock` per-ASIN
"""

from __future__ import annotations

import hashlib
import hmac
import json as _json
import logging
import os
import time
from datetime import datetime, timezone

import httpx

from backend.database import db_admin

logger = logging.getLogger(__name__)

_BASE_URL = "https://open-api.tiktokglobalshop.com"
_ENDPOINT = "/product/202309/products/search"


# ── Signing helpers ──────────────────────────────────────────────

def _sign(app_secret: str, path: str, params: dict[str, str],
          body: dict | None = None) -> str:
    """HMAC-SHA256 signature TikTok expects on every request.

    For POST requests with an application/json body, the serialized body
    is included in the payload between the sorted params and the trailing
    app_secret. Without this, TikTok returns 106001 "Invalid credentials.
    The 'sign' query parameter is invalid."
    """
    filtered = {k: v for k, v in params.items() if k not in ("sign", "access_token")}
    concat   = "".join(f"{k}{v}" for k, v in sorted(filtered.items()))
    body_str = ""
    if body is not None:
        body_str = _json.dumps(body, separators=(",", ":"))
    payload  = f"{app_secret}{path}{concat}{body_str}{app_secret}"
    return hmac.new(app_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _base_params(app_key: str, app_secret: str, shop_cipher: str, path: str,
                 body: dict | None = None) -> dict:
    p = {
        "app_key":     app_key,
        "timestamp":   str(int(time.time())),
        "shop_cipher": shop_cipher,
        "version":     "202309",
    }
    p["sign"] = _sign(app_secret, path, p, body=body)
    return p


# ── Product / SKU parsing ────────────────────────────────────────

_SKU_FIELDS = (
    "seller_sku", "external_sku_id", "merchant_sku_id",
    "reference_code", "external_sku", "sku_ref",
)


def _extract_sku(sku_obj: dict) -> str:
    """Try every known field name TikTok uses for the seller-facing SKU."""
    for f in _SKU_FIELDS:
        v = (sku_obj.get(f) or "")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _extract_stock(sku_obj: dict) -> int:
    """Sum all warehouse quantities for one SKU.

    Under `inventory` we typically get a list of {warehouse_id, quantity}.
    Some responses use `stock_infos` or a flat `available_stock` field.
    """
    total = 0
    for key in ("inventory", "stock_infos", "warehouse_stocks"):
        arr = sku_obj.get(key) or []
        if isinstance(arr, list):
            for w in arr:
                for qty_key in ("quantity", "available_stock",
                                "available_quantity", "stock"):
                    v = w.get(qty_key) if isinstance(w, dict) else None
                    if v is not None:
                        try:
                            total += int(v)
                            break
                        except (TypeError, ValueError):
                            continue
            if total > 0:
                return total
    # Flat fallbacks on the SKU itself
    for f in ("available_stock", "available_quantity", "stock", "quantity"):
        v = sku_obj.get(f)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return 0


# ── Main fetch ───────────────────────────────────────────────────

def fetch_fbt_inventory() -> tuple[int, list[str]]:
    """Query TikTok's Product Search for every product's SKU inventory and
    upsert into tiktok_stock. Returns (num_asins_updated, warnings)."""
    app_key      = os.getenv("TIKTOK_APP_KEY", "").strip()
    app_secret   = os.getenv("TIKTOK_APP_SECRET", "").strip()
    access_token = os.getenv("TIKTOK_ACCESS_TOKEN", "").strip()
    shop_cipher  = os.getenv("TIKTOK_SHOP_CIPHER", "").strip()

    if not all([app_key, app_secret, access_token, shop_cipher]):
        return 0, ["TikTok credentials not configured (need TIKTOK_APP_KEY, "
                   "TIKTOK_APP_SECRET, TIKTOK_ACCESS_TOKEN, TIKTOK_SHOP_CIPHER)"]

    warnings: list[str] = []
    all_products: list[dict] = []
    page_token = ""
    max_pages  = 30   # 30 × 100 products = 3000, way more than any shop needs

    for page_i in range(max_pages):
        body   = {"status": "ACTIVATE", "page_size": 100}
        if page_token:
            body["page_token"] = page_token
        params = _base_params(app_key, app_secret, shop_cipher, _ENDPOINT, body=body)

        try:
            # Use httpx content= with pre-serialized JSON so what we sign matches
            # what we send byte-for-byte. httpx's json= re-serializes, which can
            # subtly differ (spacing, key order) and break the signature.
            body_bytes = _json.dumps(body, separators=(",", ":")).encode()
            r = httpx.post(
                _BASE_URL + _ENDPOINT,
                params=params,
                content=body_bytes,
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

        data = payload.get("data") or {}
        prods = data.get("products") or []
        all_products.extend(prods)
        page_token = data.get("next_page_token") or ""
        if not page_token:
            break

    if not all_products:
        return 0, (warnings or ["No products returned from TikTok"])

    # Walk products → skus → aggregate per seller_sku
    per_sku: dict[str, int] = {}
    for prod in all_products:
        for sku in prod.get("skus") or []:
            key = _extract_sku(sku)
            if not key:
                continue
            per_sku[key] = per_sku.get(key, 0) + _extract_stock(sku)

    if not per_sku:
        warnings.append("No SKUs with recognized seller_sku field found in "
                        f"{len(all_products)} products. Use the debug button "
                        "to inspect raw response.")
        return 0, warnings

    # Match to ASIN via products.sku
    sku_rows = db_admin.table("products").select("asin,sku").execute().data or []
    sku_to_asin = {r["sku"]: r["asin"] for r in sku_rows if r.get("sku")}

    now_iso = datetime.now(timezone.utc).isoformat()
    updated = 0
    unknown: list[str] = []
    for sku, qty in per_sku.items():
        asin = sku_to_asin.get(sku)
        if not asin:
            unknown.append(sku)
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

    if unknown:
        warnings.append(f"{len(unknown)} SKU(s) from TikTok not in products table: "
                        f"{', '.join(unknown[:5])}"
                        + ("…" if len(unknown) > 5 else ""))

    logger.info("tiktok  %d ASINs updated (%d warnings)", updated, len(warnings))
    return updated, warnings
