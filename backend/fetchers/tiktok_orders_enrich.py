"""Enrich tiktok_finance_transactions with line-item data (SKU, product,
quantity) by calling the TikTok Order API for the order_ids present in
tiktok_finance_transactions.

Uses the same POST /order/202309/orders/search endpoint as tiktok_sales.py
(instead of GET /order/202309/orders?ids=…) because the search path and
signing is already proven in this codebase. Writes one row per
(order_id, sku_id) into tiktok_order_items.

Requires scope `seller.order.info` (already active in the Inventories
control app).
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


def _dec(v) -> float | None:
    if v is None or v == "" or v == "/":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def enrich_tiktok_orders(days_back: int = 90) -> tuple[int, list[str]]:
    """Fetch orders in the last `days_back` days and upsert their line-items
    into tiktok_order_items. Only enriches orders that appear in
    tiktok_finance_transactions, so we don't bloat the table with unpaid
    or cancelled orders. Returns (n_items_upserted, warnings)."""
    from backend.tiktok_tokens import get_tokens, refresh_and_persist

    warnings: list[str] = []
    app_key    = os.getenv("TIKTOK_APP_KEY", "").strip()
    app_secret = os.getenv("TIKTOK_APP_SECRET", "").strip()
    tokens     = get_tokens()
    access_token = tokens["access_token"]
    shop_cipher  = tokens["shop_cipher"]

    if not (app_key and app_secret and access_token and shop_cipher):
        return 0, ["TikTok credentials missing."]

    # ── Figure out which order_ids we actually care about ─────────────
    try:
        fin_rows = (
            db_admin.table("tiktok_finance_transactions")
            .select("order_adjustment_id")
            .gte("statement_date",
                 (date.today() - timedelta(days=days_back)).isoformat())
            .limit(100000)
            .execute()
            .data
        ) or []
    except Exception as exc:
        return 0, [f"Could not read tiktok_finance_transactions: {exc}"]

    want_ids: set[str] = {
        str(r["order_adjustment_id"]) for r in fin_rows
        if r.get("order_adjustment_id")
    }
    if not want_ids:
        return 0, [f"No finance rows in the last {days_back} days to enrich."]

    # ── Query the Order API for the window and keep the ones we want ──
    end   = date.today()
    start = end - timedelta(days=days_back)
    start_ts = int(datetime.combine(start, datetime.min.time(),
                                     tzinfo=timezone.utc).timestamp())
    end_ts   = int(datetime.combine(end,   datetime.max.time(),
                                     tzinfo=timezone.utc).timestamp())

    refreshed = {"once": False}

    def _call(body: dict, qs_extra: dict, token: str):
        p = {
            "app_key":     app_key,
            "timestamp":   str(int(time.time())),
            "shop_cipher": shop_cipher,
            "version":     "202309",
            **qs_extra,
        }
        p["sign"] = _sign(app_secret, _ENDPOINT, p, body=body)
        # IMPORTANT: must send the SAME bytes we signed, so serialize
        # once with the compact separators and send via content=…, not
        # json=…. httpx would add spaces → signature mismatch.
        body_bytes = _json.dumps(body, separators=(",", ":")).encode()
        return httpx.post(
            _BASE_URL + _ENDPOINT, params=p, content=body_bytes,
            headers={"x-tts-access-token": token,
                     "content-type": "application/json"},
            timeout=30,
        )

    def _request(body: dict, qs_extra: dict):
        nonlocal access_token
        r = _call(body, qs_extra, access_token)
        if (r.status_code == 401 or "Expired credentials" in r.text) and not refreshed["once"]:
            refreshed["once"] = True
            try:
                access_token = refresh_and_persist()
                r = _call(body, qs_extra, access_token)
            except Exception as exc:
                warnings.append(f"Token refresh failed: {exc}")
        return r

    body = {
        "create_time_ge": start_ts,
        "create_time_lt": end_ts,
    }
    page_token = ""
    all_orders: list[dict] = []
    # 60 pages × 50 = 3000 orders max — plenty for 3 months.
    for page_i in range(60):
        qs = {"page_size": "50"}
        if page_token:
            qs["page_token"] = page_token
        try:
            r = _request(body, qs)
        except Exception as exc:
            warnings.append(f"Orders HTTP error p{page_i}: {exc}")
            break
        if r.status_code != 200:
            warnings.append(f"Orders HTTP {r.status_code}: {r.text[:200]}")
            break
        payload = r.json()
        if payload.get("code") != 0:
            warnings.append(f"Orders API code {payload.get('code')}: {payload.get('message')}")
            break
        data = payload.get("data") or {}
        got = data.get("orders") or []
        all_orders.extend(got)
        page_token = data.get("next_page_token") or ""
        if not page_token:
            break

    warnings.append(f"Fetched {len(all_orders)} orders, kept items for "
                    f"{len(want_ids)} finance rows.")
    if not all_orders:
        return 0, warnings

    # ── Walk line_items, upsert ────────────────────────────────────────
    upserted = 0
    kept_order_ids: set[str] = set()
    for order in all_orders:
        oid = str(order.get("id") or order.get("order_id") or "").strip()
        if not oid or oid not in want_ids:
            continue
        kept_order_ids.add(oid)
        line_items = order.get("line_items") or []
        currency   = order.get("payment", {}).get("currency") or order.get("currency")

        # TikTok returns ONE line_item per unit even for qty>1 orders — so
        # roll them up per sku_id before writing.
        per_sku: dict[str, dict] = {}
        for li in line_items:
            sku_id = str(li.get("sku_id") or li.get("sku") or "").strip()
            if not sku_id:
                continue
            agg = per_sku.setdefault(sku_id, {
                "qty": 0, "sample": li,
            })
            agg["qty"] += 1  # one line_item == one unit

        for sku_id, agg in per_sku.items():
            li = agg["sample"]
            row = {
                "order_id":        oid,
                "sku_id":          sku_id,
                "product_id":      str(li.get("product_id") or "").strip() or None,
                "product_name":    (li.get("product_name") or "").strip() or None,
                "sku_name":        (li.get("sku_name") or "").strip() or None,
                "seller_sku":      (li.get("seller_sku") or "").strip() or None,
                "quantity":        agg["qty"],
                "original_price":  _dec(li.get("original_price")),
                "sale_price":      _dec(li.get("sale_price")),
                "currency":        currency,
                "raw":             li,
                "fetched_at":      datetime.now(timezone.utc).isoformat(),
            }
            try:
                db_admin.table("tiktok_order_items").upsert(
                    row, on_conflict="order_id,sku_id"
                ).execute()
                upserted += 1
            except Exception as exc:
                warnings.append(f"Save failed order={oid} sku={sku_id}: {exc}")

    missing = want_ids - kept_order_ids
    if missing:
        warnings.append(
            f"⚠️ {len(missing)} finance order_ids NOT found in the Order API "
            f"window (older than {days_back}d or different shop). Sample: "
            f"{list(missing)[:3]}"
        )

    warnings.append(f"Order items upserted: {upserted}.")
    logger.info("tiktok_orders_enrich  %d orders scanned, %d items upserted",
                len(all_orders), upserted)
    return upserted, warnings
