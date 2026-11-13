"""P&L view — consolidated monthly P&L (actuals + projected).

For PAST months (<=current month-1) we use sb_actuals_total_monthly (real data
from Sellerboard). For FUTURE months we project from assumptions:

    Revenue:
        units        = quantity (from forecast_computed OR pnl_assumptions override)
        gmv_incl_vat = units × price
        promo        = assumption (global monthly)
        gmv_net_vat_promo = gmv_incl_vat - promo
        vat          = gmv_net_vat_promo × vat_rate / (1 + vat_rate)
        sales_net    = gmv_net_vat_promo - vat

    COGS:
        product_cost = units × products.cogs_eur
        gross_profit = sales_net - product_cost
        gross_margin_pct = gross_profit / sales_net

    Amazon fees:
        tacos        = sales_net × tacos_pct          (ads)
        fulfillment  = units × fulfillment_fee_per_unit
        referral     = sales_net × referral_pct
        digital_svc  = referral × digital_services_pct
        inbound_tp   = units × inbound_transp_per_unit   (assume inbound≈sold)
        storage      = units × 2 × storage_fee_per_unit  (assume ~2mo avg stock)
        subscription = subscription_monthly (fixed)
        total_amz_fees = sum of above (except TACOS which is kept separate)

    Operating profit:
        op_profit    = gross_profit - total_amz_fees - tacos

    SG&A + MKT (all global monthly lines):
        total_sga    = labor + admin + pro_fees + tools + travel + others
        total_mkt    = agencies + reviews + ugc + others
        ebitda       = op_profit - total_sga - total_mkt
        ebitda_pct   = ebitda / gmv_net_vat_promo
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Optional

from backend.database import db_admin
from backend.pnl_assumptions import (
    LINE_ITEMS, _WILDCARD, load_all, resolve,
    get_active_scenario, get_horizon_last_ym,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _ym_tuple(ym: str) -> tuple[int, int]:
    y, m = ym.split("-")
    return int(y), int(m)


def _months_between(start_ym: str, end_ym: str) -> list[str]:
    sy, sm = _ym_tuple(start_ym)
    ey, em = _ym_tuple(end_ym)
    out = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _current_ym() -> str:
    t = date.today()
    return f"{t.year:04d}-{t.month:02d}"


# ── Data loaders ──────────────────────────────────────────────────────────

def _load_actuals() -> dict[str, dict]:
    """Return {year_month: {sales, gross_profit, net_profit, ...}} from
    sb_actuals_total_monthly."""
    rows = db_admin.table("sb_actuals_total_monthly").select("*").execute().data or []
    return {r["year_month"]: r for r in rows}


def _load_products() -> list[dict]:
    """Return all products (active + planned) with cogs."""
    try:
        rows = (
            db_admin.table("products")
            .select("asin,product_name,cogs_eur,status,launch_year_month")
            .in_("status", ["active", "planned"])
            .execute().data or []
        )
    except Exception:
        rows = []
    return rows


def _load_forecast_quantities() -> dict[tuple[str, str], int]:
    """Return {(asin, year_month): total_units} from forecast_computed,
    SUMMED across markets (we want totals per product per month).
    """
    out: dict[tuple[str, str], int] = defaultdict(int)
    try:
        page = 0
        while True:
            chunk = (
                db_admin.table("forecast_computed")
                .select("asin,year_month,units")
                .range(page * 1000, (page + 1) * 1000 - 1)
                .execute().data or []
            )
            if not chunk:
                break
            for r in chunk:
                out[(r["asin"], r["year_month"])] += int(r.get("units") or 0)
            if len(chunk) < 1000:
                break
            page += 1
            if page > 20:
                break
    except Exception:
        pass
    return out


def _resolve_quantity(
    assumptions: dict, forecast_qty: dict,
    asin: str, ym: str,
) -> float:
    """Quantity resolution (per user spec):
       1. pnl_assumptions override (scenario, asin, ym) or (asin, '*')
       2. forecast_computed (asin, ym) — only for active products
    """
    bucket = assumptions.get(("revenue", "quantity"), {})
    for key in [(asin, ym), (asin, _WILDCARD)]:
        if key in bucket:
            return bucket[key]
    return float(forecast_qty.get((asin, ym), 0))


# ── Main P&L computation ──────────────────────────────────────────────────

def compute_pnl(scenario: Optional[str] = None, months_back: int = 12) -> dict:
    """Return a full P&L structure.

    Shape:
      {
        "scenario":  "base",
        "months":    ["2025-11", ..., "2027-12"],
        "actual_ym_cutoff": "2026-10",    # last month with real data
        "lines": {
            "unit_sales":        {ym: value, ...},
            "price_avg":         {ym: value, ...},
            "gmv_incl_vat":      {ym: value, ...},
            ...
            "ebitda":            {ym: value, ...},
            "ebitda_pct":        {ym: value, ...},
        },
        "per_product_cogs":  {asin: {ym: cost}}   # for the per-product COGS section
        "per_product_units": {asin: {ym: units}}  # for the per-product unit sales section
      }
    """
    if scenario is None:
        scenario = get_active_scenario()

    actuals     = _load_actuals()
    products    = _load_products()
    forecast_q  = _load_forecast_quantities()
    assumptions = load_all(scenario)

    # Build month list: months_back past + current + up to horizon
    cur_ym = _current_ym()
    horizon = get_horizon_last_ym()
    # Start = cur_ym - months_back
    cy, cm = _ym_tuple(cur_ym)
    sm = cm - months_back
    sy = cy
    while sm < 1:
        sm += 12
        sy -= 1
    start_ym = f"{sy:04d}-{sm:02d}"
    months = _months_between(start_ym, horizon)

    # Actual cutoff = last month with Sellerboard data (typically month-1)
    actual_months = sorted(actuals.keys())
    actual_cutoff = actual_months[-1] if actual_months else ""

    def is_past(ym: str) -> bool:
        return bool(actual_cutoff) and ym <= actual_cutoff

    # ── Compute per-product units + cost for each month ────────
    per_product_units: dict[str, dict[str, float]] = defaultdict(dict)
    per_product_cost:  dict[str, dict[str, float]] = defaultdict(dict)
    cogs_by_asin = {p["asin"]: float(p.get("cogs_eur") or 0) for p in products}
    name_by_asin = {p["asin"]: p.get("product_name") or p["asin"] for p in products}

    for ym in months:
        if is_past(ym):
            # For past months we don't break units down by ASIN here — the
            # actuals come aggregated. Leave per-product empty for past months.
            continue
        for p in products:
            asin = p["asin"]
            # Planned products only sell from launch_ym onwards
            if p.get("status") == "planned":
                launch = p.get("launch_year_month")
                if launch and ym < launch:
                    continue
            qty = _resolve_quantity(assumptions, forecast_q, asin, ym)
            if qty <= 0:
                continue
            per_product_units[asin][ym] = qty
            per_product_cost[asin][ym]  = qty * cogs_by_asin.get(asin, 0.0)

    # ── Build P&L lines ────────────────────────────────────────
    lines: dict[str, dict[str, float]] = defaultdict(dict)

    # Resolve helpers
    def rget(cat: str, item: str, ym: str, asin: str = _WILDCARD, default: float = 0.0) -> float:
        return resolve(assumptions, cat, item, asin, ym, default)

    for ym in months:
        if is_past(ym):
            a = actuals.get(ym, {})
            lines["unit_sales"][ym]        = float(a.get("units") or 0)
            lines["gmv_incl_vat"][ym]      = float(a.get("sales_eur") or 0)
            # Sellerboard sales_eur is NET of VAT; we treat it as "gmv_excl_vat"
            lines["gmv_excl_vat"][ym]      = float(a.get("sales_eur") or 0)
            lines["vat"][ym]               = abs(float(a.get("vat") or 0))
            lines["gmv_incl_vat_calc"][ym] = lines["gmv_excl_vat"][ym] + lines["vat"][ym]
            lines["promo"][ym]             = abs(float((a.get("raw_json") or {}).get("promo") or 0))
            lines["product_cost"][ym]      = abs(float(a.get("product_cost") or 0))
            lines["gross_profit"][ym]      = float(a.get("gross_profit") or 0)
            lines["gross_margin_pct"][ym]  = (
                lines["gross_profit"][ym] / lines["gmv_excl_vat"][ym]
                if lines["gmv_excl_vat"][ym] else 0.0
            )
            lines["tacos_cost"][ym]        = abs(float(a.get("ads_spend") or 0))
            lines["amazon_fees_total"][ym] = abs(float(a.get("amazon_fees") or 0))
            lines["ebitda"][ym]            = float(a.get("net_profit") or 0)
            lines["ebitda_pct"][ym]        = (
                lines["ebitda"][ym] / lines["gmv_excl_vat"][ym]
                if lines["gmv_excl_vat"][ym] else 0.0
            )
            lines["manual_opex"][ym]       = float(a.get("manual_opex") or 0)
            # Price avg = sales / units (actuals)
            lines["price_avg"][ym]         = (
                lines["gmv_excl_vat"][ym] / lines["unit_sales"][ym]
                if lines["unit_sales"][ym] else 0.0
            )
        else:
            # ── Projected month ──
            total_units      = 0.0
            total_gmv_incl   = 0.0
            # Aggregate across products using their per-product price
            for p in products:
                asin = p["asin"]
                qty  = per_product_units.get(asin, {}).get(ym, 0.0)
                if qty <= 0:
                    continue
                price = rget("revenue", "price", ym, asin, 0.0)
                total_units    += qty
                total_gmv_incl += qty * price

            promo     = rget("revenue", "promo_eur", ym, _WILDCARD, 0.0)
            vat_rate  = rget("revenue", "vat_rate",  ym, _WILDCARD, 0.10)
            gmv_net_promo = max(0.0, total_gmv_incl - promo)
            vat = gmv_net_promo * vat_rate / (1 + vat_rate) if vat_rate > 0 else 0.0
            sales_net = gmv_net_promo - vat

            # COGS = sum of (units × cogs) per product
            product_cost = sum(per_product_cost.get(p["asin"], {}).get(ym, 0.0)
                               for p in products)
            gross_profit = sales_net - product_cost

            # Amazon fees
            tacos        = sales_net * rget("amz_fees", "tacos_pct", ym, _WILDCARD, 0.20)
            fulfillment  = total_units * rget("amz_fees", "fulfillment_fee_per_unit", ym, _WILDCARD, 3.73)
            referral     = sales_net * rget("amz_fees", "referral_pct", ym, _WILDCARD, 0.15)
            digital_svc  = referral  * rget("amz_fees", "digital_services_pct", ym, _WILDCARD, 0.03)
            inbound_tp   = total_units * rget("amz_fees", "inbound_transp_per_unit", ym, _WILDCARD, 0.10)
            # Assume ~2 months of stock → avg stored = units × 2
            storage      = total_units * 2 * rget("amz_fees", "storage_fee_per_unit", ym, _WILDCARD, 0.03)
            subscription = rget("amz_fees", "subscription_monthly", ym, _WILDCARD, 39.0)
            amz_fees_total = fulfillment + referral + digital_svc + inbound_tp + storage + subscription

            # SG&A + MKT (all global monthly)
            total_sga = sum(rget("sga", k, ym, _WILDCARD, 0.0)
                            for k in ["labor", "admin", "pro_fees", "tools", "travel", "others"])
            total_mkt = sum(rget("mkt", k, ym, _WILDCARD, 0.0)
                            for k in ["agencies", "reviews", "ugc", "others"])

            ebitda = gross_profit - amz_fees_total - tacos - total_sga - total_mkt

            lines["unit_sales"][ym]        = total_units
            lines["price_avg"][ym]         = (total_gmv_incl / total_units) if total_units else 0.0
            lines["gmv_incl_vat"][ym]      = total_gmv_incl
            lines["gmv_incl_vat_calc"][ym] = total_gmv_incl
            lines["promo"][ym]             = promo
            lines["vat"][ym]               = vat
            lines["gmv_excl_vat"][ym]      = sales_net
            lines["product_cost"][ym]      = product_cost
            lines["gross_profit"][ym]      = gross_profit
            lines["gross_margin_pct"][ym]  = (gross_profit / sales_net) if sales_net else 0.0
            lines["tacos_cost"][ym]        = tacos
            lines["fulfillment_cost"][ym]  = fulfillment
            lines["referral_cost"][ym]     = referral
            lines["digital_services"][ym]  = digital_svc
            lines["inbound_transp"][ym]    = inbound_tp
            lines["storage_cost"][ym]      = storage
            lines["subscription"][ym]      = subscription
            lines["amazon_fees_total"][ym] = amz_fees_total
            lines["total_sga"][ym]         = total_sga
            lines["total_mkt"][ym]         = total_mkt
            lines["ebitda"][ym]            = ebitda
            lines["ebitda_pct"][ym]        = (ebitda / sales_net) if sales_net else 0.0

    return {
        "scenario":          scenario,
        "months":            months,
        "actual_ym_cutoff":  actual_cutoff,
        "lines":             dict(lines),
        "per_product_units": {a: dict(v) for a, v in per_product_units.items()},
        "per_product_cost":  {a: dict(v) for a, v in per_product_cost.items()},
        "product_names":     name_by_asin,
    }
