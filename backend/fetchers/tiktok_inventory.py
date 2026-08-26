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
        # TikTok expects page_size / page_token as QUERY params, not body.
        # Body holds the filter payload (status, category, etc.).
        body   = {"status": "ACTIVATE"}
        extra_qs = {"page_size": "100"}
        if page_token:
            extra_qs["page_token"] = page_token

        # Merge extras into base params BEFORE signing (they participate in sig).
        p = {
            "app_key":     app_key,
            "timestamp":   str(int(time.time())),
            "shop_cipher": shop_cipher,
            "version":     "202309",
            **extra_qs,
        }
        p["sign"] = _sign(app_secret, _ENDPOINT, p, body=body)
        params = p

        try:
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

    # Walk products → skus. Two ways to identify each row:
    #   1. seller_sku match against products.sku (works when TikTok returns it)
    #   2. tiktok_product_id → asin manual mapping (fallback when seller_sku
    #      is empty, which is the common case for many shops)
    sku_rows = db_admin.table("products").select("asin,sku").execute().data or []
    sku_to_asin = {r["sku"]: r["asin"] for r in sku_rows if r.get("sku")}

    # Load the manual mapping table (silently empty if the table doesn't exist yet)
    try:
        map_rows = db_admin.table("tiktok_product_map").select(
            "tiktok_product_id,asin"
        ).execute().data or []
        product_id_to_asin = {r["tiktok_product_id"]: r["asin"] for r in map_rows}
    except Exception:
        product_id_to_asin = {}
        warnings.append("tiktok_product_map table not found — run the SQL "
                        "to enable manual mapping fallback.")

    # Aggregate stock per resolved ASIN (either via seller_sku or product-id map)
    per_asin: dict[str, int] = {}
    unmapped_products: list[dict] = []
    for prod in all_products:
        prod_id = str(prod.get("id") or "")
        # Try seller_sku match on any of the SKUs of this product
        matched_via_sku = False
        prod_stock = 0
        for sku in prod.get("skus") or []:
            key = _extract_sku(sku)
            qty = _extract_stock(sku)
            prod_stock += qty
            if key and key in sku_to_asin:
                asin = sku_to_asin[key]
                per_asin[asin] = per_asin.get(asin, 0) + qty
                matched_via_sku = True
        if matched_via_sku:
            continue
        # Fallback: use the tiktok_product_id → asin mapping
        if prod_id and prod_id in product_id_to_asin:
            asin = product_id_to_asin[prod_id]
            per_asin[asin] = per_asin.get(asin, 0) + prod_stock
            continue
        # Neither worked — surface it for the user to map
        unmapped_products.append({
            "id":    prod_id,
            "title": prod.get("title", "")[:80],
            "stock": prod_stock,
        })

    now_iso = datetime.now(timezone.utc).isoformat()
    updated = 0
    for asin, qty in per_asin.items():
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

    if unmapped_products:
        warnings.append(
            f"{len(unmapped_products)} TikTok product(s) not matched to an ASIN. "
            "Use the 🔗 Map TikTok Products tab on the TikTok page to assign them."
        )

    logger.info("tiktok  %d ASINs updated (%d warnings)", updated, len(warnings))
    return updated, warnings


def fetch_tiktok_products_for_mapping() -> list[dict]:
    """Return every TikTok product with (id, title, stock) for the mapping UI.

    Uses the same API call as fetch_fbt_inventory but returns raw rows so the
    dashboard can render a table where the user picks an ASIN for each.
    """
    app_key      = os.getenv("TIKTOK_APP_KEY", "").strip()
    app_secret   = os.getenv("TIKTOK_APP_SECRET", "").strip()
    access_token = os.getenv("TIKTOK_ACCESS_TOKEN", "").strip()
    shop_cipher  = os.getenv("TIKTOK_SHOP_CIPHER", "").strip()

    if not all([app_key, app_secret, access_token, shop_cipher]):
        return []

    all_products: list[dict] = []
    page_token = ""
    for _ in range(30):
        body   = {"status": "ACTIVATE"}
        extra_qs = {"page_size": "100"}
        if page_token:
            extra_qs["page_token"] = page_token
        p = {
            "app_key": app_key, "timestamp": str(int(time.time())),
            "shop_cipher": shop_cipher, "version": "202309",
            **extra_qs,
        }
        p["sign"] = _sign(app_secret, _ENDPOINT, p, body=body)
        body_bytes = _json.dumps(body, separators=(",", ":")).encode()
        try:
            r = httpx.post(
                _BASE_URL + _ENDPOINT, params=p, content=body_bytes,
                headers={"x-tts-access-token": access_token,
                         "content-type": "application/json"},
                timeout=15,
            )
            if r.status_code != 200:
                break
            payload = r.json()
            if payload.get("code") != 0:
                break
            data = payload.get("data") or {}
            all_products.extend(data.get("products") or [])
            page_token = data.get("next_page_token") or ""
            if not page_token:
                break
        except Exception:
            break

    # Simplify for the UI: id, title, total stock
    rows = []
    for prod in all_products:
        stock = 0
        for sku in prod.get("skus") or []:
            stock += _extract_stock(sku)
        rows.append({
            "tiktok_product_id": str(prod.get("id") or ""),
            "title":             prod.get("title", ""),
            "stock":             stock,
        })
    return rows
