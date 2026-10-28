"""Fetch TikTok Shop Finance statements + transactions → upsert into
tiktok_finance_transactions (transaction-level, equivalent to the "Order
details" tab of the Excel reports from Finance Center → Statements).

Two-step flow:
  1. GET /finance/202309/statements           → list statement IDs in window
  2. GET /finance/202309/statements/{id}/statement_transactions
                                              → line-level rows per statement

Requires `seller.finance.info` scope. Same HMAC signing and 401 auto-refresh
pattern as tiktok_sales.py.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from datetime import date, datetime, timezone, timedelta

import httpx

from backend.database import db_admin

logger = logging.getLogger(__name__)

_BASE_URL         = "https://open-api.tiktokglobalshop.com"
_STATEMENTS_PATH  = "/finance/202309/statements"
_TRANSACTIONS_FMT = "/finance/202309/statements/{statement_id}/statement_transactions"


def _sign(app_secret: str, path: str, params: dict) -> str:
    filtered = {k: v for k, v in params.items() if k not in ("sign", "access_token")}
    concat   = "".join(f"{k}{v}" for k, v in sorted(filtered.items()))
    payload  = f"{app_secret}{path}{concat}{app_secret}"
    return hmac.new(app_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _dec(v) -> float | None:
    if v is None or v == "" or v == "/":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    if v is None or v == "" or v == "/":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _str(v) -> str | None:
    if v is None or v == "" or v == "/":
        return None
    s = str(v).strip()
    return s or None


def _dt_from_ts(ts) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return None


# Map snake_case DB columns ← camelCase/alt names from the TikTok API payload.
# TikTok's field names shift between versions; we try several per column and
# fall back to None. The full payload is kept in `raw` so columns we miss
# today can be backfilled later with a SQL update, no re-fetch needed.
_FIELD_MAP: list[tuple[str, list[str], str]] = [
    # (db_column, api_candidate_names, type: 'dec'|'int'|'str'|'date_ts')
    ("payment_id",                                   ["payment_id"], "str"),
    ("status",                                       ["status", "statement_status"], "str"),
    ("currency",                                     ["currency"], "str"),
    ("sku_id",                                       ["sku_id"], "str"),
    ("quantity",                                     ["quantity"], "int"),
    ("product_name",                                 ["product_name"], "str"),
    ("sku_name",                                     ["sku_name"], "str"),
    ("order_created_date",                           ["order_create_time", "order_created_time"], "date_ts"),
    ("order_delivery_date",                          ["order_delivery_time"], "date_ts"),

    ("total_settlement_amount",                      ["settlement_amount", "total_settlement_amount"], "dec"),
    ("net_sales",                                    ["net_sales_amount", "net_sales"], "dec"),
    ("subtotal_before_discounts",                    ["subtotal_before_discounts"], "dec"),
    ("seller_discounts",                             ["seller_discounts"], "dec"),
    ("refund_subtotal_before_seller_discounts",      ["refund_subtotal_before_seller_discounts"], "dec"),
    ("refund_of_seller_discounts",                   ["refund_of_seller_discounts"], "dec"),

    ("shipping",                                     ["shipping_amount", "shipping"], "dec"),
    ("tiktok_shop_shipping_fee",                     ["tiktok_shop_shipping_fee"], "dec"),
    ("fulfilled_by_tiktok_shop_shipping_fee",        ["fulfilled_by_tiktok_shop_shipping_fee"], "dec"),
    ("customer_shipping_fee",                        ["customer_shipping_fee"], "dec"),
    ("refunded_customer_shipping_fee",               ["refunded_customer_shipping_fee"], "dec"),
    ("tiktok_shop_shipping_incentive",               ["tiktok_shop_shipping_incentive"], "dec"),
    ("tiktok_shop_shipping_incentive_refund",        ["tiktok_shop_shipping_incentive_refund"], "dec"),
    ("shipping_subsidy",                             ["shipping_subsidy"], "dec"),
    ("actual_return_shipping_fee",                   ["actual_return_shipping_fee"], "dec"),
    ("shipping_fee_discount",                        ["shipping_fee_discount"], "dec"),
    ("return_shipping_label_fee",                    ["return_shipping_label_fee"], "dec"),
    ("return_shipping_fee_reimbursement",            ["return_shipping_fee_reimbursement"], "dec"),
    ("fee_for_fbt_free_shipping",                    ["fee_for_fbt_free_shipping"], "dec"),

    ("fees",                                         ["fee_amount", "fees"], "dec"),
    ("tiktok_shop_commission_fee",                   ["tiktok_shop_commission_fee"], "dec"),
    ("affiliate_commission",                         ["affiliate_commission"], "dec"),
    ("affiliate_partner_commission",                 ["affiliate_partner_commission"], "dec"),
    ("affiliate_shop_ads_commission",                ["affiliate_shop_ads_commission"], "dec"),
    ("epr_pay_on_behalf_service_fee",                ["epr_pay_on_behalf_service_fee"], "dec"),
    ("co_funded_promotion_seller_funded",            ["co_funded_promotion_seller_funded"], "dec"),
    ("smart_promotion_fee_tax",                      ["smart_promotion_fee_tax"], "dec"),
    ("affiliate_commission_deposit",                 ["affiliate_commission_deposit"], "dec"),
    ("affiliate_commission_refund",                  ["affiliate_commission_refund"], "dec"),
    ("affiliate_partner_shop_ads_commission",        ["affiliate_partner_shop_ads_commission"], "dec"),
    ("campaign_resource_fee",                        ["campaign_resource_fee"], "dec"),
    ("tax_and_duty",                                 ["tax_and_duty"], "dec"),
    ("smart_promotion_fee",                          ["smart_promotion_fee"], "dec"),
    ("campaign_service_fee",                         ["campaign_service_fee"], "dec"),
    ("managed_service_plan_sales_tax",               ["managed_service_plan_sales_tax"], "dec"),
    ("managed_service_plan_per_order_fee",           ["managed_service_plan_per_order_fee"], "dec"),
    ("gmv_max_ad_fee",                               ["gmv_max_ad_fee"], "dec"),
    ("auto_post_shoppable_video_commission_fee",     ["auto_post_shoppable_video_commission_fee"], "dec"),

    ("adjustment_amount",                            ["adjustment_amount"], "dec"),
    ("adjustment_reason",                            ["adjustment_reason"], "str"),
    ("related_order_id",                             ["related_order_id"], "str"),

    ("customer_payment",                             ["customer_payment"], "dec"),
    ("customer_refund",                              ["customer_refund"], "dec"),
    ("seller_co_funded_voucher_discount",            ["seller_co_funded_voucher_discount"], "dec"),
    ("seller_co_funded_voucher_discount_refund",     ["seller_co_funded_voucher_discount_refund"], "dec"),
    ("platform_discounts",                           ["platform_discounts"], "dec"),
    ("platform_discounts_refund",                    ["platform_discounts_refund"], "dec"),
    ("platform_co_funded_voucher_discounts",         ["platform_co_funded_voucher_discounts"], "dec"),
    ("platform_co_funded_voucher_discounts_refund",  ["platform_co_funded_voucher_discounts_refund"], "dec"),
    ("customer_paid_shipping_fee_before_discounts",  ["customer_paid_shipping_fee_before_discounts"], "dec"),
    ("seller_shipping_fee_discount",                 ["seller_shipping_fee_discount"], "dec"),
    ("tiktok_shop_shipping_fee_discount_to_customer",["tiktok_shop_shipping_fee_discount_to_customer"], "dec"),

    ("estimated_chargeable_package_weight",          ["estimated_chargeable_package_weight"], "dec"),
    ("chargeable_package_weight",                    ["chargeable_package_weight"], "dec"),
    ("collection_methods",                           ["collection_methods"], "str"),
    ("delivery_option",                              ["delivery_option"], "str"),
    ("mode_of_tiktok_shop_shipping_fee_discount",    ["mode_of_tiktok_shop_shipping_fee_discount"], "str"),
]


def _coerce(tx: dict, api_names: list[str], kind: str):
    for name in api_names:
        if name in tx:
            v = tx[name]
            if kind == "dec":     return _dec(v)
            if kind == "int":     return _int(v)
            if kind == "str":     return _str(v)
            if kind == "date_ts": return _dt_from_ts(v)
    return None


def fetch_tiktok_finance(days_back: int = 90) -> tuple[int, list[str]]:
    """Fetch finance transactions for the last `days_back` days and upsert
    into tiktok_finance_transactions. Returns (n_rows_upserted, warnings).

    Default 90 days covers 3 monthly statement cycles — enough to re-pull
    late-posted adjustments without re-downloading everything.
    """
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

    end   = date.today()
    start = end - timedelta(days=days_back)
    start_ts = int(datetime.combine(start, datetime.min.time(),
                                     tzinfo=timezone.utc).timestamp())
    end_ts   = int(datetime.combine(end,   datetime.max.time(),
                                     tzinfo=timezone.utc).timestamp())

    refreshed = {"once": False}

    def _call(method: str, path: str, extra_qs: dict, token: str):
        p = {
            "app_key":     app_key,
            "timestamp":   str(int(time.time())),
            "shop_cipher": shop_cipher,
            "version":     "202309",
            **extra_qs,
        }
        p["sign"] = _sign(app_secret, path, p)
        return httpx.request(
            method, _BASE_URL + path, params=p,
            headers={"x-tts-access-token": token, "content-type": "application/json"},
            timeout=30,
        )

    def _request(method: str, path: str, extra_qs: dict):
        nonlocal access_token
        r = _call(method, path, extra_qs, access_token)
        if (r.status_code == 401 or "Expired credentials" in r.text) and not refreshed["once"]:
            refreshed["once"] = True
            try:
                access_token = refresh_and_persist()
                r = _call(method, path, extra_qs, access_token)
            except Exception as exc:
                warnings.append(f"Token refresh failed: {exc}")
        return r

    # ── Step 1: list statements in window ──────────────────────────────
    statements: list[dict] = []
    page_token = ""
    for page_i in range(100):
        qs = {
            "page_size":         "50",
            "statement_time_ge": str(start_ts),
            "statement_time_lt": str(end_ts),
        }
        if page_token:
            qs["page_token"] = page_token
        try:
            r = _request("GET", _STATEMENTS_PATH, qs)
        except Exception as exc:
            warnings.append(f"Statements list HTTP error p{page_i}: {exc}")
            break
        if r.status_code != 200:
            warnings.append(f"Statements list HTTP {r.status_code}: {r.text[:200]}")
            break
        body = r.json()
        if body.get("code") != 0:
            warnings.append(f"Statements list API code {body.get('code')}: {body.get('message')}")
            break
        data = body.get("data") or {}
        statements.extend(data.get("statements") or [])
        page_token = data.get("next_page_token") or ""
        if not page_token:
            break

    warnings.append(f"Window: {start} → {end}. Found {len(statements)} statements.")
    if not statements:
        return 0, warnings

    # ── Step 2: fetch transactions for each statement ──────────────────
    upserted = 0
    total_tx = 0
    for stmt in statements:
        stmt_id   = (stmt.get("id") or stmt.get("statement_id") or "").strip()
        if not stmt_id:
            continue
        stmt_date = _dt_from_ts(stmt.get("statement_time"))
        tx_path   = _TRANSACTIONS_FMT.format(statement_id=stmt_id)
        tx_page_token = ""
        for page_i in range(200):
            qs = {"page_size": "50"}
            if tx_page_token:
                qs["page_token"] = tx_page_token
            try:
                r = _request("GET", tx_path, qs)
            except Exception as exc:
                warnings.append(f"Tx HTTP error stmt={stmt_id} p{page_i}: {exc}")
                break
            if r.status_code != 200:
                warnings.append(f"Tx HTTP {r.status_code} stmt={stmt_id}: {r.text[:200]}")
                break
            body = r.json()
            if body.get("code") != 0:
                warnings.append(f"Tx API code {body.get('code')} stmt={stmt_id}: {body.get('message')}")
                break
            data = body.get("data") or {}
            txs  = data.get("statement_transactions") or data.get("transactions") or []
            total_tx += len(txs)
            for tx in txs:
                tx_order_id = _str(tx.get("order_id") or tx.get("adjustment_id") or tx.get("id"))
                tx_type     = _str(tx.get("type")) or "unknown"
                if not tx_order_id:
                    continue
                row = {
                    "statement_id":        stmt_id,
                    "order_adjustment_id": tx_order_id,
                    "type":                tx_type,
                    "shop_cipher":         shop_cipher,
                    "statement_date":      stmt_date,
                    "raw":                 tx,
                    "fetched_at":          datetime.now(timezone.utc).isoformat(),
                }
                for db_col, api_names, kind in _FIELD_MAP:
                    row[db_col] = _coerce(tx, api_names, kind)
                try:
                    db_admin.table("tiktok_finance_transactions").upsert(
                        row, on_conflict="statement_id,order_adjustment_id,type"
                    ).execute()
                    upserted += 1
                except Exception as exc:
                    warnings.append(f"Save failed stmt={stmt_id} tx={tx_order_id}: {exc}")
            tx_page_token = data.get("next_page_token") or ""
            if not tx_page_token:
                break

    warnings.append(f"Transactions scanned: {total_tx}. Rows upserted: {upserted}.")
    logger.info("tiktok_finance  %d statements, %d transactions, %d upserted",
                len(statements), total_tx, upserted)
    return upserted, warnings
