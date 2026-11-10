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
        ("price",             "€ Consumer Price (INCL. IVA)",    True,  "Precio al cliente CON IVA (es lo que ves en Amazon). El IVA se descuenta más abajo automáticamente. Para productos con descuento constante, pon aquí el precio efectivo promedio."),
        ("quantity",          "📊 Expected units sold",          True,  "Unidades esperadas por mes. Para productos existentes se usa el forecast automático. Para planned products se pone manual aquí (seed carga ramp-up típico de lanzamiento)."),
        ("promo_eur",         "€ Promo / Vine / descuento",      False, "Línea mensual global (igual que tu P&L sheet). Monto total esperado de promos + Vine en cada mes. Para un lanzamiento con Vine, suma el Vine del nuevo producto al mes correspondiente."),
        ("vat_rate",          "% VAT",                            False, "IVA promedio (ej. 10% = 0.10) — se usa para sacar el IVA del Consumer Price"),
    ],
    # COGS is read directly from products.cogs_eur (ONE source of truth).
    # UI shows a dedicated editable table in the COGS tab — no items here.
    "cogs": [],
    "amz_fees": [
        ("tacos_pct",         "% TACOS (ads / sales)",           False, "Spend de PPC esperado como % de Sales. Línea global (igual que el P&L sheet). Default 20%."),
        ("amazon_fees_pct",   "% Amazon fees (ex-ads)",          False, "Commission + FBA + Shipping + Refund costs como % de Sales. Default ~35% (basado en histórico Monvita). Línea global."),
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

# ── Planned products ──────────────────────────────────────────────────────

# Ramp-up curve (base scenario) based on real Monvita launches
_LAUNCH_RAMP_BASE = {1: 120, 2: 200, 3: 250, 4: 190, 5: 150}
_STABLE_MONTH_6_PLUS = 180

# Vine cost per launch (€170/country × 4 + 30u × 4 countries × €6 unit cost)
_VINE_FEE_EUR            = 680   # €170 × 4 markets
_VINE_FREE_UNITS_COST_EUR = 720  # 30 × 4 × €6 avg unit cost
VINE_TOTAL_PER_PRODUCT   = _VINE_FEE_EUR + _VINE_FREE_UNITS_COST_EUR  # €1400

# 2027 launch schedule: 2 products every 2 months
LAUNCH_SCHEDULE_2027 = [
    ("2027-01", ["PLANNED-001", "PLANNED-002"]),
    ("2027-03", ["PLANNED-003", "PLANNED-004"]),
    ("2027-05", ["PLANNED-005", "PLANNED-006"]),
    ("2027-07", ["PLANNED-007", "PLANNED-008"]),
    ("2027-09", ["PLANNED-009", "PLANNED-010"]),
    ("2027-11", ["PLANNED-011", "PLANNED-012"]),
]


def _ym_add(ym: str, months_to_add: int) -> str:
    y, m = (int(x) for x in ym.split("-"))
    total = y * 12 + (m - 1) + months_to_add
    return f"{total // 12:04d}-{(total % 12) + 1:02d}"


def _ramp_units(month_of_life: int, scenario: str) -> int:
    base = _LAUNCH_RAMP_BASE.get(month_of_life, _STABLE_MONTH_6_PLUS)
    mult = {"bear": 0.80, "base": 1.0, "bull": 1.20}.get(scenario, 1.0)
    return int(round(base * mult))


def seed_planned_products_2027(
    default_price: float = 29.90,
    default_unit_cost: float = 6.0,
    default_unit_label_cost: float = 0.5,
) -> tuple[int, list[str]]:
    """Create 12 planned products in the products table + seed their
    expected quantity per month per scenario (bear/base/bull) + their
    Vine budget in the global promo_eur line of the launch month.

    Idempotent — upserts by ASIN. Returns (n_products, warnings)."""
    warnings: list[str] = []
    products_written = 0
    horizon_end = get_horizon_last_ym()

    for launch_ym, asins in LAUNCH_SCHEDULE_2027:
        for i, asin in enumerate(asins):
            # 1. Upsert product
            try:
                db_admin.table("products").upsert({
                    "asin": asin,
                    "product_name": f"Planned product {asin.split('-')[1]} (lanzamiento {launch_ym})",
                    "status": "planned",
                    "launch_year_month": launch_ym,
                    "cogs_eur": round(default_unit_cost + default_unit_label_cost, 2),
                    "lead_time_days": 70,
                    "target_days_coverage": 60,
                }, on_conflict="asin").execute()
                products_written += 1
            except Exception as exc:
                warnings.append(f"{asin}: upsert failed: {exc}")
                continue

            # 2. Seed pnl_assumptions: price per scenario (unit cost goes to
            #    products.cogs_eur via the upsert above — ONE source of truth)
            for scenario in SCENARIOS:
                save_value(scenario, "revenue", "price",
                           asin, _WILDCARD, default_price,
                           notes="planned product seed")

                # 3. Quantity ramp-up: month 1 is launch_ym, then +1, +2, ...
                m_of_life = 1
                cur_ym = launch_ym
                while cur_ym <= horizon_end:
                    units = _ramp_units(m_of_life, scenario)
                    save_value(scenario, "revenue", "quantity",
                               asin, cur_ym, float(units),
                               notes="planned product ramp-up")
                    cur_ym = _ym_add(cur_ym, 1)
                    m_of_life += 1

    # 4. For each launch month, bump the global promo_eur to include Vine for
    #    both products launched that month (2 products × €1400 = €2800 extra).
    #    We PRESERVE any existing promo_eur value by SUMMING vs overriding.
    #    Note: we add Vine to BASE scenario only; bear/bull adjust manually.
    for launch_ym, asins in LAUNCH_SCHEDULE_2027:
        vine_total = VINE_TOTAL_PER_PRODUCT * len(asins)   # €2800 for 2 products
        for scenario in SCENARIOS:
            # Load current override (if any) for this scenario+month
            existing = (
                db_admin.table("pnl_assumptions")
                .select("value")
                .eq("scenario", scenario)
                .eq("category", "revenue")
                .eq("item_name", "promo_eur")
                .eq("asin", _WILDCARD)
                .eq("year_month", launch_ym)
                .execute().data or []
            )
            cur_val = float(existing[0]["value"]) if existing else 0.0
            # Bump Vine ONTO existing value (so you can set a base 'other promo'
            # amount separately and we don't nuke it)
            save_value(scenario, "revenue", "promo_eur",
                       _WILDCARD, launch_ym, cur_val + vine_total,
                       notes=f"auto-seed: +€{vine_total:.0f} Vine for {len(asins)} planned products")

    return products_written, warnings


def seed_global_defaults() -> int:
    """Seed sensible defaults into pnl_assumptions for base scenario globals
    (TACOS 20%, VAT 10%, etc.). Idempotent via upsert. Returns rows written."""
    # Defaults derived from Monvita historical (Oct 2026):
    #   TACOS avg = Advertising / Sales = 11,280 / 69,873 ≈ 16% (bumped to 20%
    #     as a planning conservative buffer; user can override)
    #   Amazon fees (ex ads) = (Amazon_fees + Shipping) / Sales
    #                        = 24,844 / 69,873 ≈ 35.5%
    #   VAT = 10% average across markets
    defaults = [
        ("revenue",   "vat_rate",         0.10),
        ("amz_fees",  "tacos_pct",        0.20),
        ("amz_fees",  "amazon_fees_pct",  0.355),
    ]
    written = 0
    for cat, item, val in defaults:
        save_value("base", cat, item, _WILDCARD, _WILDCARD, float(val),
                   notes="auto-seed default")
        written += 1
    return written
