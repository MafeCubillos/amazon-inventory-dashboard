"""P&L Assumptions store + resolver.

Table: pnl_assumptions
Columns: id, scenario, category, item_name, asin, year_month, value, notes.

Resolution falls through 4 levels (specific → general):
    1. (scenario, item_name, asin, year_month)       exact per-product per-month
    2. (scenario, item_name, asin,          '*')     per-product default
    3. (scenario, item_name, '*',  year_month)       global override for a month
    4. (scenario, item_name, '*',          '*')      global default

Category → item_name canonical catalog
  revenue   : price, promo_eur, vat_rate
  cogs      : unit_cost, unit_label_cost
  amz_fees  : tacos_pct, fba_fee_per_unit, storage_fee_per_unit, referral_pct
  sga       : labor, admin, pro_fees, tools, travel, others
  mkt       : agencies, reviews, ugc, others
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from backend.database import db_admin


SCENARIOS = ["bear", "base", "bull"]

LINE_ITEMS = {
    "revenue": [
        ("price",             "€ Consumer Price (INCL. IVA)",    True,  "Precio al cliente CON IVA (es lo que ves en Amazon). El IVA se descuenta más abajo automáticamente."),
        ("promo_eur",         "€ Promo / Vine / descuento",      True,  "Monto esperado mensual de promos y Vine (se resta de GMV)"),
        ("vat_rate",          "% VAT",                            False, "IVA promedio (ej. 10% = 0.10) — se usa para sacar el IVA del Consumer Price"),
    ],
    "cogs": [
        ("unit_cost",         "€ Unit cost (supplier)",          True,  "Costo supplier por unidad"),
        ("unit_label_cost",   "€ Unit label cost",               True,  "Costo etiqueta por unidad"),
    ],
    "amz_fees": [
        ("tacos_pct",         "% TACOS (ads / sales)",           True,  "Spend PPC / Sales esperado"),
        ("fba_fee_per_unit",  "€ FBA fulfillment / unidad",      True,  "Fee FBA por unidad vendida"),
        ("referral_pct",      "% Referral (Amazon commission)",   True,  "Comisión Amazon sobre sales (ej 15%)"),
        ("storage_fee_pct",   "% FBA storage of Sales",           False, "Storage fees aprox como % de Sales"),
    ],
    "sga": [
        ("labor",             "€ Labor (sueldos)",               False, "Sueldos y cargas mensuales"),
        ("admin",             "€ Admin",                          False, "Admin general"),
        ("pro_fees",          "€ ProFees (gestorías, legales)",  False, "Honorarios profesionales"),
        ("tools",             "€ Tools (SaaS)",                   False, "Sellerboard, Helium, Hoded, ..."),
        ("travel",            "€ Travel",                         False, "Viajes"),
        ("others",            "€ Others SG&A",                    False, "Otros gastos generales"),
    ],
    "mkt": [
        ("agencies",          "€ Agencies",                       False, "Agencias de marketing"),
        ("reviews",           "€ Reviews",                        False, "Programas de reviews"),
        ("ugc",               "€ UGC",                            False, "User-generated content"),
        ("others",            "€ Others MKT",                     False, "Otros mkt"),
    ],
}

_WILDCARD = "*"


# ── Config ────────────────────────────────────────────────────────────────

def get_config(key: str, default: str = "") -> str:
    try:
        rows = db_admin.table("pnl_config").select("value").eq("key", key).execute().data or []
        return (rows[0]["value"] if rows else default) or default
    except Exception:
        return default


def set_config(key: str, value: str) -> None:
    db_admin.table("pnl_config").upsert(
        {"key": key, "value": value}, on_conflict="key"
    ).execute()


def get_active_scenario() -> str:
    s = get_config("active_scenario", "base").lower()
    return s if s in SCENARIOS else "base"


def get_horizon_last_ym() -> str:
    return get_config("horizon_until", "2027-12") or "2027-12"


# ── Assumptions read / write ──────────────────────────────────────────────

def load_all(scenario: str | None = None) -> dict:
    """Return nested dict:
        out[(category, item_name)][(asin, year_month)] = value
    Falls through resolution in lookup helpers below.
    """
    q = db_admin.table("pnl_assumptions").select("*")
    if scenario:
        q = q.eq("scenario", scenario)
    rows = q.execute().data or []
    out: dict = defaultdict(dict)
    for r in rows:
        out[(r["category"], r["item_name"])][(r["asin"], r["year_month"])] = float(r["value"])
    return out


def resolve(
    assumptions: dict,
    category: str,
    item_name: str,
    asin: str,
    year_month: str,
    default: float = 0.0,
) -> float:
    """Fallthrough: exact → per-product-default → per-month-global → global-default."""
    bucket = assumptions.get((category, item_name), {})
    for key in [
        (asin,       year_month),
        (asin,       _WILDCARD),
        (_WILDCARD,  year_month),
        (_WILDCARD,  _WILDCARD),
    ]:
        if key in bucket:
            return bucket[key]
    return default


def save_value(
    scenario: str, category: str, item_name: str,
    asin: str, year_month: str, value: float, notes: str | None = None,
) -> None:
    row = {
        "scenario":   scenario,
        "category":   category,
        "item_name":  item_name,
        "asin":       asin or _WILDCARD,
        "year_month": year_month or _WILDCARD,
        "value":      round(float(value), 6),
        "notes":      notes,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    db_admin.table("pnl_assumptions").upsert(
        row, on_conflict="scenario,category,item_name,asin,year_month"
    ).execute()


def delete_value(scenario: str, category: str, item_name: str, asin: str, year_month: str) -> None:
    (
        db_admin.table("pnl_assumptions")
        .delete()
        .eq("scenario", scenario)
        .eq("category", category)
        .eq("item_name", item_name)
        .eq("asin", asin or _WILDCARD)
        .eq("year_month", year_month or _WILDCARD)
        .execute()
    )


# ── Future months horizon ─────────────────────────────────────────────────

def future_months(start_ym: str | None = None) -> list[str]:
    """Months from start_ym (defaults to current + 1) up to horizon_until."""
    from datetime import date
    end = get_horizon_last_ym()
    end_y, end_m = (int(x) for x in end.split("-"))
    if start_ym:
        y, m = (int(x) for x in start_ym.split("-"))
    else:
        today = date.today()
        y, m = today.year, today.month + 1
        if m > 12:
            m = 1
            y += 1
    out = []
    while (y, m) <= (end_y, end_m):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


# ── Seed helpers ──────────────────────────────────────────────────────────

def seed_global_defaults() -> int:
    """Seed sensible defaults into pnl_assumptions for base scenario globals
    (TACOS 20%, VAT 10%, etc.). Idempotent via upsert. Returns rows written."""
    defaults = [
        # Revenue
        ("revenue",   "vat_rate",      0.10),   # 10% VAT average
        # Amazon fees
        ("amz_fees",  "tacos_pct",     0.20),   # 20% TACOS
        ("amz_fees",  "referral_pct",  0.15),   # 15% referral (Amazon commission)
        ("amz_fees",  "storage_fee_pct", 0.005),
        ("amz_fees",  "fba_fee_per_unit", 3.50),
        # COGS defaults (zero — user must fill per product)
        # SG&A and MKT default to 0 — user fills them per month
    ]
    written = 0
    for cat, item, val in defaults:
        save_value("base", cat, item, _WILDCARD, _WILDCARD, float(val),
                   notes="auto-seed default")
        written += 1
    return written
