"""Sellerboard CSV fetcher — pulls two monthly reports via their automation
URL feeds and loads them into Supabase.

Two sources:
  - SELLERBOARD_URL_MONTH    → one row per month (whole-account totals,
                               including the user's manual opex lines)
  - SELLERBOARD_URL_PRODUCT  → per-day per-ASIN per-marketplace rows for the
                               previous month; we aggregate to monthly.

Targets:
  - sb_actuals_total_monthly  ← whole-account monthly P&L inputs
  - sb_actuals_monthly        ← per-product per-market monthly P&L inputs

Call `sync_sellerboard()` to run both. Idempotent: upserts on PKs.
"""

from __future__ import annotations

import csv
import io
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

import requests

from backend.database import db_admin

logger = logging.getLogger(__name__)


# Known Amazon system fee / ads columns (Sellerboard stores these as NEGATIVE
# numbers for costs). We sum them and keep them negative.
_ADS_COLS = [
    "SponsoredProducts", "SponsoredDisplay", "SponsoredBrands",
    "SponsoredBrandsVideo", "Google ads", "Facebook ads",
]
_AMZ_FEE_COLS = [
    "Commission", "COMPENSATED_CLAWBACK", "DealParticipationFee",
    "DealPerformanceFee", "DigitalServicesFee", "DigitalServicesFeeFBA",
    "FBAInboundTransportationFee", "FBAInboundTransportationProgramFee",
    "FBALongTermStorageFee", "FBAPerUnitFulfillmentFee", "FBARemovalFee",
    "FBAStorageFee", "REVERSAL_REIMBURSEMENT", "Subscription",
    "WAREHOUSE_LOST", "GiftWrap", "Shipping",
]
_PRODUCT_COST_COLS = [
    "ProductCost Sales", "ProductCost Unsellable Refunds",
    "ProductCost Non-Amazon", "ProductCost MissingFromInbound",
    "ProductCost MultichannelCosts", "ProductCost CostOfMissingReturns",
]
# All the standard Sellerboard columns we know about in the month CSV.
# Everything else that isn't these is treated as a user-added manual
# expense/opex line (e.g. "Agencia Amazon", "Cuota autónomo", etc.).
_KNOWN_MONTH_COLS = {
    "DateFrom", "DateTo",
    "SalesOrganic", "SalesPPC", "SalesSponsoredProducts", "SalesSponsoredDisplay",
    "UnitsOrganic", "UnitsPPC", "UnitsSponsoredProducts", "UnitsSponsoredDisplay",
    "Orders", "Refunds", "PromoValue", "Value of returned items",
    "EstimatedPayout", "VAT", "GrossProfit", "NetProfit",
    "Margin", "Real ACOS", "Sessions", "Unit Session Percentage",
} | set(_ADS_COLS) | set(_AMZ_FEE_COLS) | set(_PRODUCT_COST_COLS)
# Refund columns start with "Refund " — also considered part of Amazon fees
_REFUND_COL_PREFIX = "Refund "

_MARKET_NORMALIZE = {
    "Amazon.es": "ES", "Amazon.fr": "FR", "Amazon.de": "DE", "Amazon.it": "IT",
    "Amazon.com.be": "BE", "Amazon.ie": "IE", "Amazon.nl": "NL",
}


def _to_float(x) -> float:
    try:
        if x is None or x == "":
            return 0.0
        return float(str(x).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _to_int(x) -> int:
    try:
        return int(_to_float(x))
    except Exception:
        return 0


def _parse_date_to_ym(date_str: str) -> str:
    """Parse '01/10/2026' (dd/mm/yyyy) → '2026-10'. Fall back to raw if unparseable."""
    if not date_str:
        return ""
    try:
        dt = datetime.strptime(date_str.strip(), "%d/%m/%Y")
        return dt.strftime("%Y-%m")
    except Exception:
        return date_str[:7] if len(date_str) >= 7 else date_str


def _clean_key(k: str) -> str:
    """Strip BOM, quotes and whitespace from CSV header keys."""
    return (k or "").lstrip("﻿").strip().strip('"').strip()


def _download_csv(url: str) -> str:
    """Fetch a Sellerboard automation URL as CSV text."""
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.text


# ── Parsers ────────────────────────────────────────────────────────────────

def parse_month_csv(text: str) -> list[dict]:
    """Parse the whole-account-per-month CSV into rows for sb_actuals_total_monthly.
    Returns a list of upsert-ready dicts.
    """
    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [_clean_key(f) for f in (reader.fieldnames or [])]
    out: list[dict] = []
    for row in reader:
        row = {_clean_key(k): v for k, v in row.items()}
        ym = _parse_date_to_ym(row.get("DateFrom", ""))
        if not ym:
            continue

        sales = (_to_float(row.get("SalesOrganic")) + _to_float(row.get("SalesPPC"))
                 + _to_float(row.get("SalesSponsoredProducts"))
                 + _to_float(row.get("SalesSponsoredDisplay")))
        units = (_to_int(row.get("UnitsOrganic")) + _to_int(row.get("UnitsPPC"))
                 + _to_int(row.get("UnitsSponsoredProducts"))
                 + _to_int(row.get("UnitsSponsoredDisplay")))
        ads   = sum(_to_float(row.get(c)) for c in _ADS_COLS)
        # Amazon fees + refund-related entries all get aggregated
        amz_fees = sum(_to_float(row.get(c)) for c in _AMZ_FEE_COLS)
        amz_fees += sum(_to_float(v) for k, v in row.items() if k.startswith(_REFUND_COL_PREFIX))
        pcost = sum(_to_float(row.get(c)) for c in _PRODUCT_COST_COLS)

        # Manual opex: everything else not in our known-standard set
        manual: dict[str, float] = {}
        for k, v in row.items():
            if k in _KNOWN_MONTH_COLS or k.startswith(_REFUND_COL_PREFIX):
                continue
            val = _to_float(v)
            if val != 0:
                manual[k] = val
        manual_opex_total = sum(manual.values())

        out.append({
            "year_month":       ym,
            "sales_eur":        round(sales, 2),
            "units":            units,
            "orders":           _to_int(row.get("Orders")),
            "refunds_units":    _to_int(row.get("Refunds")),
            "ads_spend":        round(ads, 2),
            "amazon_fees":      round(amz_fees, 2),
            "product_cost":     round(pcost, 2),
            "vat":              round(_to_float(row.get("VAT")), 2),
            "gross_profit":     round(_to_float(row.get("GrossProfit")), 2),
            "net_profit":       round(_to_float(row.get("NetProfit")), 2),
            "manual_opex":      round(manual_opex_total, 2),
            "manual_breakdown": manual,
            "raw_json":         {k: v for k, v in row.items()},
            "updated_at":       datetime.now(timezone.utc).isoformat(),
        })
    return out


def parse_product_csv(text: str) -> list[dict]:
    """Parse the per-day per-ASIN per-market CSV and AGGREGATE to monthly rows
    for sb_actuals_monthly. One row per (asin, marketplace, year_month).
    """
    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [_clean_key(f) for f in (reader.fieldnames or [])]

    agg: dict[tuple, dict] = {}
    raw_sample: dict[tuple, dict] = {}
    for row in reader:
        row = {_clean_key(k): v for k, v in row.items()}
        ym   = _parse_date_to_ym(row.get("Date", ""))
        asin = (row.get("ASIN") or "").strip()
        mp_raw = (row.get("Marketplace") or "").strip()
        mp = _MARKET_NORMALIZE.get(mp_raw, mp_raw)
        if not (ym and asin and mp):
            continue
        key = (asin, mp, ym)
        d = agg.setdefault(key, {
            "units": 0, "sales_eur": 0.0, "promo_eur": 0.0,
            "refunds_units": 0, "ads_spend": 0.0, "amazon_fees": 0.0,
            "product_cost": 0.0, "vat": 0.0,
            "gross_profit": 0.0, "net_profit": 0.0,
        })
        d["units"]         += (_to_int(row.get("UnitsOrganic")) + _to_int(row.get("UnitsPPC"))
                               + _to_int(row.get("UnitsSponsoredProducts"))
                               + _to_int(row.get("UnitsSponsoredDisplay")))
        d["sales_eur"]     += (_to_float(row.get("SalesOrganic")) + _to_float(row.get("SalesPPC"))
                               + _to_float(row.get("SalesSponsoredProducts"))
                               + _to_float(row.get("SalesSponsoredDisplay")))
        d["promo_eur"]     += _to_float(row.get("PromoValue"))
        d["refunds_units"] += _to_int(row.get("Refunds"))
        d["ads_spend"]     += sum(_to_float(row.get(c)) for c in _ADS_COLS)
        d["amazon_fees"]   += sum(_to_float(row.get(c)) for c in _AMZ_FEE_COLS)
        d["amazon_fees"]   += sum(_to_float(v) for k, v in row.items() if k.startswith(_REFUND_COL_PREFIX))
        d["product_cost"]  += sum(_to_float(row.get(c)) for c in _PRODUCT_COST_COLS)
        d["vat"]           += _to_float(row.get("VAT"))
        d["gross_profit"]  += _to_float(row.get("GrossProfit"))
        d["net_profit"]    += _to_float(row.get("NetProfit"))
        raw_sample.setdefault(key, {k: v for k, v in row.items()})

    now_iso = datetime.now(timezone.utc).isoformat()
    out: list[dict] = []
    for (asin, mp, ym), d in agg.items():
        out.append({
            "asin":          asin,
            "marketplace":   mp,
            "year_month":    ym,
            "units":         d["units"],
            "sales_eur":     round(d["sales_eur"], 2),
            "promo_eur":     round(d["promo_eur"], 2),
            "refunds_units": d["refunds_units"],
            "ads_spend":     round(d["ads_spend"], 2),
            "amazon_fees":   round(d["amazon_fees"], 2),
            "product_cost":  round(d["product_cost"], 2),
            "vat":           round(d["vat"], 2),
            "gross_profit":  round(d["gross_profit"], 2),
            "net_profit":    round(d["net_profit"], 2),
            "raw_json":      raw_sample.get((asin, mp, ym)),
            "updated_at":    now_iso,
        })
    return out


# ── Sync orchestrator ─────────────────────────────────────────────────────

def sync_sellerboard() -> tuple[int, int, list[str]]:
    """Fetch both reports, write to Supabase. Returns:
        (n_product_rows, n_month_rows, warnings)
    """
    warnings: list[str] = []
    url_month   = os.getenv("SELLERBOARD_URL_MONTH", "").strip()
    url_product = os.getenv("SELLERBOARD_URL_PRODUCT", "").strip()

    if not url_month or not url_product:
        warnings.append("SELLERBOARD_URL_MONTH / SELLERBOARD_URL_PRODUCT not set in env/secrets.")
        return 0, 0, warnings

    # 1. Product-level (per ASIN, per market, per month)
    try:
        product_text = _download_csv(url_product)
        product_rows = parse_product_csv(product_text)
    except Exception as exc:
        warnings.append(f"Product CSV fetch/parse failed: {exc}")
        product_rows = []

    n_product = 0
    for i in range(0, len(product_rows), 500):
        chunk = product_rows[i:i + 500]
        try:
            db_admin.table("sb_actuals_monthly").upsert(
                chunk, on_conflict="asin,marketplace,year_month"
            ).execute()
            n_product += len(chunk)
        except Exception as exc:
            warnings.append(f"sb_actuals_monthly upsert chunk {i//500}: {exc}")

    # 2. Account-total (per month)
    try:
        month_text = _download_csv(url_month)
        month_rows = parse_month_csv(month_text)
    except Exception as exc:
        warnings.append(f"Month CSV fetch/parse failed: {exc}")
        month_rows = []

    n_month = 0
    for i in range(0, len(month_rows), 100):
        chunk = month_rows[i:i + 100]
        try:
            db_admin.table("sb_actuals_total_monthly").upsert(
                chunk, on_conflict="year_month"
            ).execute()
            n_month += len(chunk)
        except Exception as exc:
            warnings.append(f"sb_actuals_total_monthly upsert chunk {i//100}: {exc}")

    logger.info("sellerboard  product rows upserted: %d, month rows: %d", n_product, n_month)
    return n_product, n_month, warnings


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO)
    n_p, n_m, warns = sync_sellerboard()
    print(f"✅ Product rows: {n_p}")
    print(f"✅ Month rows:   {n_m}")
    for w in warns:
        print(f"⚠️  {w}")
