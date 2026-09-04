"""Fetch last-30-days TikTok orders → aggregate to per-SKU velocity → upsert.

Uses TikTok Shop Order Search API. Requires `seller.order.info` scope (which
returns customer PII we discard — we only read line_items.seller_sku +
quantity for velocity math).

Endpoint: POST /order/202309/orders/search
Docs:    https://partner.tiktokshop.com/docv2/page/650a0d0dbb0f720289cec44b

Auto-refreshes access token on 401 using backend.tiktok_tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import json as _json
import logging
import os
import time
from datetime import date, datetime, timezone, timedelta

import httpx

from backend.database import db_admin

logger = logging.getLogger(__name__)

_BASE_URL = "https://open-api.tiktokglobalshop.com"
_ENDPOINT = "/order/202309/orders/search"


def _sign(app_secret: str, path: str, params: dict, body: dict | None = None) -> str:
    filtered = {k: v for k, v in params.items() if k not in ("sign", "access_token")}
    concat   = "".join(f"{k}{v}" for k, v in sorted(filtered.items()))
    body_str = _json.dumps(body, separators=(",", ":")) if body is not None else ""
    payload  = f"{app_secret}{path}{concat}{body_str}{app_secret}"
    return hmac.new(app_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _sku_from_line_item(item: dict) -> str:
    for f in ("seller_sku", "external_sku_id", "merchant_sku_id",
              "reference_code", "external_sku", "sku_ref"):
        v = (item.get(f) or "")
        if isinstance(v, str) and v.strip():
            return v.strip()
    sku_obj = item.get("sku") or {}
    for f in ("seller_sku", "external_sku_id", "merchant_sku_id"):
        v = (sku_obj.get(f) or "")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _qty_from_line_item(item: dict) -> int:
    for f in ("quantity", "sku_quantity", "unit_count"):
        v = item.get(f)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return 0


def fetch_tiktok_sales() -> tuple[int, list[str]]:
    """Fetch last 30 days of TikTok orders, aggregate by SKU → ASIN, upsert
    into tiktok_velocity. Returns (n_asins_updated, warnings)."""
    from backend.tiktok_tokens import get_tokens, refresh_and_persist

    warnings: list[str] = []
    app_key    = os.getenv("TIKTOK_APP_KEY", "").strip()
    app_secret = os.getenv("TIKTOK_APP_SECRET", "").strip()
    tokens     = get_tokens()
    access_token = tokens["access_token"]
    shop_cipher  = tokens["shop_cipher"]

    if not (app_key and app_secret and access_token and shop_cipher):
        return 0, ["TikTok credentials missing (need APP_KEY, APP_SECRET, "
                   "and tokens in Supabase tiktok_tokens or env fallback)."]

    # Time window — last 30 complete days
    today = date.today()
    end   = today - timedelta(days=1)   # yesterday (avoid partial day)
    start = end   - timedelta(days=29)  # 30 days inclusive
    start_ts = int(datetime.combine(start, datetime.min.time(),
                                     tzinfo=timezone.utc).timestamp())
    end_ts   = int(datetime.combine(end,   datetime.max.time(),
                                     tzinfo=timezone.utc).timestamp())

    refreshed_once = False

    def _do_request(token: str, page_tok: str):
        # Body: order status = COMPLETED / SHIPPING / IN_TRANSIT / etc.
        # For sales velocity we want COMPLETED orders (not cancelled).
        body = {
            "order_status":   "COMPLETED",
            "create_time_ge": start_ts,
            "create_time_lt": end_ts,
        }
        extra_qs = {"page_size": "50"}
        if page_tok:
            extra_qs["page_token"] = page_tok
        p = {
            "app_key":     app_key,
            "timestamp":   str(int(time.time())),
            "shop_cipher": shop_cipher,
            "version":     "202309",
            **extra_qs,
        }
        p["sign"] = _sign(app_secret, _ENDPOINT, p, body=body)
        body_bytes = _json.dumps(body, separators=(",", ":")).encode()
        return httpx.post(
            _BASE_URL + _ENDPOINT, params=p, content=body_bytes,
            headers={"x-tts-access-token": token, "content-type": "application/json"},
            timeout=20,
        )

    all_orders: list[dict] = []
    page_token = ""
    for page_i in range(200):   # 200 × 50 = 10k orders — plenty for 30 days
        try:
            r = _do_request(access_token, page_token)
        except Exception as exc:
            warnings.append(f"HTTP error on page {page_i}: {exc}")
            break

        # Auto-refresh on 401
        if (r.status_code == 401 or "Expired credentials" in r.text) and not refreshed_once:
            refreshed_once = True
            try:
                access_token = refresh_and_persist()
                r = _do_request(access_token, page_token)
            except Exception as exc:
                warnings.append(f"Token refresh failed: {exc}")
                break

        if r.status_code != 200:
            warnings.append(f"HTTP {r.status_code} on page {page_i}: {r.text[:200]}")
            break

        payload = r.json()
        if payload.get("code") != 0:
            warnings.append(f"API code {payload.get('code')}: {payload.get('message')}")
            break

        data = payload.get("data") or {}
        got = data.get("orders") or []
        all_orders.extend(got)
        page_token = data.get("next_page_token") or ""
        if not page_token:
            break

    if not all_orders:
        return 0, (warnings or [f"No orders in the last 30 days ({start} → {end})"])

    # Aggregate units per SKU from line_items
    per_sku: dict[str, int] = {}
    for order in all_orders:
        for li in (order.get("line_items") or []):
            sku = _sku_from_line_item(li)
            qty = _qty_from_line_item(li)
            if sku and qty:
                per_sku[sku] = per_sku.get(sku, 0) + qty

    logger.info("tiktok_sales  %d orders → %d unique SKUs sold in last 30d",
                len(all_orders), len(per_sku))

    # Load ASIN mapping — try seller_sku first, fall back to tiktok_product_map
    sku_rows = db_admin.table("products").select("asin,sku").execute().data or []
    sku_to_asin = {r["sku"]: r["asin"] for r in sku_rows if r.get("sku")}

    now_iso = datetime.now(timezone.utc).isoformat()
    per_asin: dict[str, int] = {}
    unmapped: list[str] = []
    for sku, qty in per_sku.items():
        asin = sku_to_asin.get(sku)
        if not asin:
            unmapped.append(sku)
            continue
        per_asin[asin] = per_asin.get(asin, 0) + qty

    updated = 0
    for asin, units in per_asin.items():
        try:
            db_admin.table("tiktok_velocity").upsert({
                "asin":            asin,
                "units_sold_30d":  int(units),
                "velocity_daily":  round(units / 30, 4),
                "period_end_date": end.isoformat(),
                "updated_at":      now_iso,
            }, on_conflict="asin").execute()
            updated += 1
        except Exception as exc:
            warnings.append(f"Failed to save {asin}: {exc}")

    if unmapped:
        warnings.append(f"{len(unmapped)} TikTok SKU(s) sold but not mapped to any ASIN: "
                        f"{', '.join(unmapped[:5])}"
                        + ("…" if len(unmapped) > 5 else ""))

    return updated, warnings
