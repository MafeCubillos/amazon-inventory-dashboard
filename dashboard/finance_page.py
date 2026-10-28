"""TikTok Finance page — view and export transaction-level finance data
pulled by backend.fetchers.tiktok_finance.

Keeps the UI minimal for Phase 1: date range, Sync button, transactions
table, Export CSV in the exact column order of TikTok's "Order details"
sheet so the output can be pasted directly into the Google Sheet P&L.
"""

from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd
import streamlit as st


# Column order + display names matching the "Order details" sheet of the
# Excel reports downloaded from Finance Center → Statements. Keep this
# in sync with backend/fetchers/tiktok_finance.py _FIELD_MAP.
_ORDER_DETAILS_COLUMNS: list[tuple[str, str]] = [
    ("statement_date",                               "Statement date"),
    ("statement_id",                                 "Statement ID"),
    ("payment_id",                                   "Payment ID"),
    ("status",                                       "Status"),
    ("currency",                                     "Currency"),
    ("type",                                         "Type"),
    ("order_adjustment_id",                          "Order/adjustment ID"),
    ("sku_id",                                       "SKU ID"),
    ("quantity",                                     "Quantity"),
    ("product_name",                                 "Product name"),
    ("sku_name",                                     "SKU name"),
    ("order_created_date",                           "Order created date"),
    ("order_delivery_date",                          "Order delivery date"),
    ("total_settlement_amount",                      "Total settlement amount"),
    ("net_sales",                                    "Net sales"),
    ("subtotal_before_discounts",                    "Subtotal before discounts"),
    ("seller_discounts",                             "Seller discounts"),
    ("refund_subtotal_before_seller_discounts",      "Refund subtotal before seller discounts"),
    ("refund_of_seller_discounts",                   "Refund of seller discounts"),
    ("shipping",                                     "Shipping"),
    ("tiktok_shop_shipping_fee",                     "TikTok Shop shipping fee"),
    ("fulfilled_by_tiktok_shop_shipping_fee",        "Fulfilled by TikTok Shop shipping fee"),
    ("customer_shipping_fee",                        "Customer shipping fee"),
    ("refunded_customer_shipping_fee",               "Refunded customer shipping fee"),
    ("tiktok_shop_shipping_incentive",               "TikTok Shop shipping incentive"),
    ("tiktok_shop_shipping_incentive_refund",        "TikTok Shop shipping incentive refund"),
    ("shipping_subsidy",                             "Shipping subsidy"),
    ("actual_return_shipping_fee",                   "Actual return shipping fee"),
    ("shipping_fee_discount",                        "Shipping fee discount"),
    ("return_shipping_label_fee",                    "Return shipping label fee"),
    ("return_shipping_fee_reimbursement",            "Return shipping fee reimbursement"),
    ("fee_for_fbt_free_shipping",                    "Fee for FBT free shipping"),
    ("fees",                                         "Fees"),
    ("tiktok_shop_commission_fee",                   "TikTok Shop commission fee"),
    ("affiliate_commission",                         "Affiliate Commission"),
    ("affiliate_partner_commission",                 "Affiliate partner commission"),
    ("affiliate_shop_ads_commission",                "Affiliate Shop Ads commission"),
    ("epr_pay_on_behalf_service_fee",                "EPR Pay on Behalf service fee"),
    ("co_funded_promotion_seller_funded",            "Co-funded promotion (seller-funded)"),
    ("smart_promotion_fee_tax",                      "Smart Promotion fee tax"),
    ("affiliate_commission_deposit",                 "Affiliate commission deposit"),
    ("affiliate_commission_refund",                  "Affiliate commission refund"),
    ("affiliate_partner_shop_ads_commission",        "Affiliate Partner shop ads commission"),
    ("campaign_resource_fee",                        "Campaign resource fee"),
    ("tax_and_duty",                                 "Tax and duty"),
    ("smart_promotion_fee",                          "Smart Promotion fee"),
    ("campaign_service_fee",                         "Campaign service fee"),
    ("managed_service_plan_sales_tax",               "Managed service plan (Sales tax)"),
    ("managed_service_plan_per_order_fee",           "Managed service plan (Per order fee)"),
    ("gmv_max_ad_fee",                               "GMV Max ad fee"),
    ("auto_post_shoppable_video_commission_fee",     "Auto-post shoppable video commission fee"),
    ("adjustment_amount",                            "Adjustment amount"),
    ("adjustment_reason",                            "Adjustment reason"),
    ("related_order_id",                             "Related order ID"),
    ("customer_payment",                             "Customer payment"),
    ("customer_refund",                              "Customer refund"),
    ("seller_co_funded_voucher_discount",            "Seller co-funded voucher discount"),
    ("seller_co_funded_voucher_discount_refund",     "Seller co-funded voucher discount refund"),
    ("platform_discounts",                           "Platform discounts"),
    ("platform_discounts_refund",                    "Platform discounts refund"),
    ("platform_co_funded_voucher_discounts",         "Platform co-funded voucher discounts"),
    ("platform_co_funded_voucher_discounts_refund",  "Platform co-funded voucher discounts refund"),
    ("customer_paid_shipping_fee_before_discounts",  "Customer-paid shipping fee before discounts"),
    ("seller_shipping_fee_discount",                 "Seller shipping fee discount"),
    ("tiktok_shop_shipping_fee_discount_to_customer","TikTok Shop shipping fee discount to customer"),
    ("estimated_chargeable_package_weight",          "Estimated chargeable package weight"),
    ("chargeable_package_weight",                    "Chargeable package weight"),
    ("collection_methods",                           "Collection methods"),
    ("delivery_option",                              "Delivery option"),
    ("mode_of_tiktok_shop_shipping_fee_discount",    "Mode of TikTok Shop shipping fee discount to customer"),
]


def _load_transactions(start: date, end: date) -> pd.DataFrame:
    """Load finance rows whose statement_date falls in [start, end] —
    that matches the user's Google Sheet, which buckets by statement date."""
    from backend.database import db_admin
    rows = (
        db_admin.table("tiktok_finance_transactions")
        .select("*")
        .gte("statement_date", start.isoformat())
        .lte("statement_date", end.isoformat())
        .order("statement_date", desc=True)
        .limit(50000)
        .execute()
        .data
    ) or []
    return pd.DataFrame(rows)


def _order_details_df(df: pd.DataFrame) -> pd.DataFrame:
    """Project to the same column names & order as TikTok's Order details."""
    if df.empty:
        return pd.DataFrame(columns=[label for _, label in _ORDER_DETAILS_COLUMNS])
    out = pd.DataFrame()
    for db_col, label in _ORDER_DETAILS_COLUMNS:
        out[label] = df[db_col] if db_col in df.columns else None
    return out


# ── P&L structure — mirrors the user's Google Sheet exactly ────────
# Each row is one of:
#   {"raw": "field_name"}                     sum raw->>field (all types)
#   {"raw": "field_name", "types": [...]}     sum raw->>field for those types
#   {"settlement_for": "TYPE_NAME"}           sum settlement_amount for that type
#   {"derived": "smart_promotion"}            fee_amount − detail fees (Fees-only
#                                             residual that TikTok hides inside
#                                             fee_amount)
#   "SUM"                                     computed sum of this section's
#                                             detail rows (shown bold/gray)
#
# The row key SOURCE_SPEC is what tells the pivot how to derive the value.
# This structure is the ONLY place the mapping lives. Change a label, add
# a new row, or point a row at a different raw field without touching code.
_NON_ORDER_TYPES = [
    "FBT_WAREHOUSE_SERVICE_FEE", "LOGISTICS_REIMBURSEMENT",
    "LOGISTICS_DEDUCTION", "NET_EARNINGS_BALANCE",
    "TOP_UP_ADS_BALANCE_USING_GMV_PAYMENTS",
    "BILL_PAYMENT_FOR_NEGATIVE_BALANCE",
    "BILL_DEDUCTION_FOR_NEGATIVE_BALANCE",
    "CHARGEBACK", "OTHER_ADJUSTMENT",
    "TAX_ADJUSTMENT", "SELLER_MISSION_REWARD",
    "PROMOTION_ADJUSTMENT",
]

_PL_SECTIONS: list[tuple[str, list[tuple[str, object]]]] = [
    ("Totales", [
        ("Total settlement amount",                 {"raw": "settlement_amount"}),
    ]),
    ("Net sales", [
        # Section total uses TikTok's own net_sales_amount; subrows are per-field.
        ("Net sales",                               {"raw": "net_sales_amount",                  "types": ["ORDER"]}),
        ("Subtotal before discounts",               {"raw": "gross_sales_amount",                "types": ["ORDER"]}),
        ("Seller discounts",                        {"raw": "seller_discount_amount",            "types": ["ORDER"]}),
        ("Refund subtotal before seller discounts", {"raw": "gross_sales_refund_amount",         "types": ["ORDER"]}),
        ("Refund of seller discounts",              {"raw": "seller_discount_refund_amount",     "types": ["ORDER"]}),
    ]),
    ("Shipping", [
        # Section total uses TikTok's own shipping_cost_amount (per-order
        # shipping subtotal). Matches Report's "Shipping" exactly.
        ("Shipping",                                {"raw": "shipping_cost_amount",              "types": ["ORDER"]}),
        ("TikTok Shop shipping fee",                {"raw": "shipping_fee_amount",               "types": ["ORDER"]}),
        ("Fulfilled by TikTok Shop shipping fee",   {"raw": "fbt_shipping_cost_amount",          "types": ["ORDER"]}),
        ("Customer shipping fee",                   {"raw": "customer_shipping_fee_amount",      "types": ["ORDER"]}),
        ("Refunded customer shipping fee",          {"raw": "customer_paid_shipping_fee_refund_amount", "types": ["ORDER"]}),
        ("TikTok Shop shipping incentive",          {"raw": "shipping_cost_discount_amount",     "types": ["ORDER"]}),
        ("TikTok Shop shipping incentive refund",   {"raw": "refund_shipping_cost_discount_amount","types": ["ORDER"]}),
        ("Shipping subsidy",                        {"raw": "shipping_fee_subsidy_amount",       "types": ["ORDER"]}),
        ("Actual return shipping fee",              {"raw": "actual_return_shipping_fee_amount", "types": ["ORDER"]}),
        ("Return shipping label fee",               {"raw": "return_shipping_fee_amount",        "types": ["ORDER"]}),
        # Fee for FBT free shipping = shipping_cost - fulfilled - incentive
        # - customer - refunded_customer. Verified across Aug totals and the
        # single-order sample.
        ("Fee for FBT free shipping",               {"derived": "fbt_free_shipping"}),
    ]),
    ("Fees", [
        # Section total uses TikTok's own fee_amount (per-order fees total).
        ("Fees",                                    {"raw": "fee_amount",                        "types": ["ORDER"]}),
        ("TikTok Shop commission fee",              {"raw": "platform_commission_amount",        "types": ["ORDER"]}),
        ("Affiliate Commission",                    {"raw": "affiliate_commission_amount",       "types": ["ORDER"]}),
        ("Affiliate partner commission",            {"raw": "affiliate_partner_commission_amount","types": ["ORDER"]}),
        ("Affiliate Shop Ads commission",           {"raw": "affiliate_ads_commission_amount",   "types": ["ORDER"]}),
        # Smart Promotion = fee_amount − commission − affiliate − partner − ads
        ("Smart Promotion fee",                     {"derived": "smart_promotion"}),
    ]),
    ("Adjustments", [
        # Section total = sum settlement_amount for ALL non-ORDER types
        ("Adjustments",                             {"raw": "settlement_amount", "types": _NON_ORDER_TYPES}),
        ("FBT warehouse service fee",               {"settlement_for": "FBT_WAREHOUSE_SERVICE_FEE"}),
        ("Logistics reimbursement",                 {"settlement_for": "LOGISTICS_REIMBURSEMENT"}),
        ("Logistics deduction",                     {"settlement_for": "LOGISTICS_DEDUCTION"}),
        ("Sales proceed (negative balance)",        {"settlement_for": "NET_EARNINGS_BALANCE"}),
        ("GMV Payment for TikTok Ads",              {"settlement_for": "TOP_UP_ADS_BALANCE_USING_GMV_PAYMENTS"}),
        ("Bill payment (negative balance)",         {"settlement_for": "BILL_PAYMENT_FOR_NEGATIVE_BALANCE"}),
        ("Bill deduction (negative balance)",       {"settlement_for": "BILL_DEDUCTION_FOR_NEGATIVE_BALANCE"}),
        ("Chargeback",                              {"settlement_for": "CHARGEBACK"}),
        ("Other adjustment",                        {"settlement_for": "OTHER_ADJUSTMENT"}),
    ]),
]

# Fields that already appear somewhere in the P&L sections → used by the
# "Unmapped fields" bucket to know what to EXCLUDE.
_MAPPED_RAW_FIELDS = {
    spec["raw"]
    for _, section in _PL_SECTIONS
    for _, spec in section
    if isinstance(spec, dict) and "raw" in spec
}
# Also these are already part of the structure in other ways:
_MAPPED_RAW_FIELDS |= {
    "settlement_amount",     # Totales row
    "net_sales_amount",      # Net sales row (ORDER type)
    "revenue_amount",        # Duplicated with net_sales
    "fee_amount",            # Fees row (ORDER type)
    "shipping_cost_amount",  # Shipping row (ORDER type)
    "adjustment_amount",     # Covered by settlement_for rows
    "after_seller_discounts_subtotal_amount",  # Derived: gross − discounts
    "customer_payment_amount",      # Not a P&L line (duplicate of net_sales)
    "customer_refund_amount",       # Covered by gross_sales_refund
    "customer_order_refund_amount",
    "actual_shipping_fee_amount",   # Duplicate of fbt_shipping_cost_amount
    "customer_paid_shipping_fee_amount",   # Duplicate of customer_shipping_fee
    "platform_shipping_fee_discount_amount",  # Duplicate of shipping_cost_discount
    "affiliate_commission_before_pit",     # Duplicate of affiliate_commission
    # Platform discounts: not in Report tab, part of Customer payment flow
    "platform_discount_amount",
    "platform_discount_refund_amount",
    "platform_refund_subsidy_amount",
    # PIT and tax fields (regional specifics, zero for Spain):
    "pit_amount", "iva_vat_amount", "sales_tax_amount",
    "sales_tax_refund_amount", "sales_tax_payment_amount",
    "isr_income_tax_amount", "retail_delivery_fee_amount",
    "retail_delivery_fee_refund_amount", "retail_delivery_fee_payment_amount",
    "fbm_shipping_cost_amount",   # 0 for Monvita (not Fulfilled by Merchant)
    # Promo field we track as 'TikTok Shop shipping incentive refund'
    "refund_shipping_cost_discount_amount",
    # Other fields TikTok ships but aren't P&L lines:
    "promo_shipping_incentive_amount",   # 0 — superseded by shipping_cost_discount
    "fbt_fulfillment_fee_amount",
    "fbt_fulfillment_fee_reimbursement_amount",
    "refund_administration_fee_amount",
    "signature_confirmation_fee_amount",
    "transaction_fee_amount",
    "shipping_insurance_fee_amount",
    "customer_shipping_fee_offset_amount",
    "referral_fee_amount",
    "retail_delivery_fee_refund_amount",
    # ID / metadata fields:
    "id", "order_id", "adjustment_id", "adjustment_order_id",
    "order_create_time", "type", "currency",
}


def _sum_from_raw(dfx: pd.DataFrame, field: str,
                  types: list[str] | None = None) -> dict[str, float]:
    """Return {YYYY-MM: sum of raw->>field} optionally filtered by type.
    Works even if the raw column stores dicts (jsonb from Supabase)."""
    if "raw" not in dfx.columns:
        return {}
    sub = dfx if not types else dfx[dfx["type"].isin(types)]
    if sub.empty:
        return {}

    def _val(r):
        if r is None:
            return 0.0
        try:
            v = r.get(field) if isinstance(r, dict) else None
            if v is None or v == "":
                return 0.0
            return float(v)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    vals = sub["raw"].map(_val)
    return vals.groupby(sub["month"]).sum().to_dict()


def _get_raw(r, k):
    try:
        return float(r.get(k) or 0) if isinstance(r, dict) else 0.0
    except Exception:
        return 0.0


def _derive_smart_promotion(dfx: pd.DataFrame) -> dict[str, float]:
    """Smart Promotion fee = fee_amount − (platform_commission + affiliate
    + affiliate_partner + affiliate_ads). TikTok hides this inside fee_amount
    on ORDER rows without exposing it as its own field."""
    orders = dfx[dfx["type"] == "ORDER"]
    if orders.empty:
        return {}
    fee = orders["raw"].map(lambda r: _get_raw(r, "fee_amount"))
    others = (
        orders["raw"].map(lambda r: _get_raw(r, "platform_commission_amount")) +
        orders["raw"].map(lambda r: _get_raw(r, "affiliate_commission_amount")) +
        orders["raw"].map(lambda r: _get_raw(r, "affiliate_partner_commission_amount")) +
        orders["raw"].map(lambda r: _get_raw(r, "affiliate_ads_commission_amount"))
    )
    diff = fee - others
    return diff.groupby(orders["month"]).sum().to_dict()


def _derive_fbt_free_shipping(dfx: pd.DataFrame) -> dict[str, float]:
    """Fee for FBT free shipping = shipping_cost_amount
       − fbt_shipping_cost_amount
       − shipping_cost_discount_amount
       − customer_shipping_fee_amount
       − customer_paid_shipping_fee_refund_amount
    (verified against Aug totals: -155.18 + 447.34 − 322.52 − 21.36 + 3.99
     = −47.73, matches user's sheet)."""
    orders = dfx[dfx["type"] == "ORDER"]
    if orders.empty:
        return {}
    s = orders["raw"].map(lambda r: _get_raw(r, "shipping_cost_amount"))
    fbt = orders["raw"].map(lambda r: _get_raw(r, "fbt_shipping_cost_amount"))
    inc = orders["raw"].map(lambda r: _get_raw(r, "shipping_cost_discount_amount"))
    cs  = orders["raw"].map(lambda r: _get_raw(r, "customer_shipping_fee_amount"))
    rcs = orders["raw"].map(lambda r: _get_raw(r, "customer_paid_shipping_fee_refund_amount"))
    diff = s - fbt - inc - cs - rcs
    return diff.groupby(orders["month"]).sum().to_dict()


def _unmapped_fields_sums(dfx: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Discover ALL numeric raw fields that are non-zero AND not in the
    _MAPPED_RAW_FIELDS set. Returns {field: {month: sum}} sorted by
    |total|. This is the safety net for new TikTok fields."""
    if "raw" not in dfx.columns or dfx.empty:
        return {}
    # Union of keys across all rows (cheap for a few thousand rows)
    all_keys: set[str] = set()
    for r in dfx["raw"].dropna():
        if isinstance(r, dict):
            all_keys.update(r.keys())
    unknown = sorted(all_keys - _MAPPED_RAW_FIELDS)
    out: dict[str, dict[str, float]] = {}
    for k in unknown:
        sums = _sum_from_raw(dfx, k, None)
        total = sum(sums.values())
        if abs(total) >= 0.01:
            out[k] = sums
    # Sort by |total| descending
    return dict(sorted(out.items(),
                       key=lambda kv: -abs(sum(kv[1].values()))))


def _units_by_month(fin_df: pd.DataFrame,
                    month_source: str = "order_created_date"
                    ) -> tuple[dict[str, int], pd.DataFrame]:
    """Returns totals and per-product unit counts by month. month_source
    picks which date column drives the pivot."""
    empty = ({}, pd.DataFrame())
    if fin_df.empty or month_source not in fin_df.columns:
        return empty
    order_ids = fin_df["order_adjustment_id"].dropna().astype(str).unique().tolist()
    items = _load_order_items(order_ids)
    if items.empty:
        return empty
    fin_small = (
        fin_df[["order_adjustment_id", month_source]]
        .rename(columns={"order_adjustment_id": "order_id"})
        .copy()
    )
    fin_small["order_id"] = fin_small["order_id"].astype(str)
    merged = items.merge(fin_small, on="order_id", how="left")
    merged["month"] = pd.to_datetime(merged[month_source]).dt.strftime("%Y-%m")
    merged = merged.dropna(subset=["month"])
    totals = merged.groupby("month")["quantity"].sum().astype(int).to_dict()

    # Per-product breakdown (short label from product_name or sku_name)
    label = (merged["product_name"].fillna(merged["sku_name"])
                                    .fillna(merged["sku_id"]))
    merged["producto"] = label
    per_product = (
        merged.pivot_table(index="producto", columns="month",
                           values="quantity", aggfunc="sum", fill_value=0)
    )
    return totals, per_product


def _pl_pivot(df: pd.DataFrame,
              units_total: dict[str, int],
              units_per_product: pd.DataFrame,
              month_source: str = "statement_date") -> pd.DataFrame:
    """Build a P&L DataFrame: rows = metrics, columns = months.
    Pulls every value from raw JSON (so new TikTok fields are never
    silently ignored — they land in the 'Unmapped' section). Section
    SUM rows are computed from the detail rows below them."""
    if df.empty or month_source not in df.columns:
        return pd.DataFrame()
    dfx = df.copy()
    dfx["month"] = pd.to_datetime(dfx[month_source]).dt.strftime("%Y-%m")
    all_months = sorted(dfx["month"].dropna().unique().tolist())

    def _spec_values(spec) -> dict[str, float]:
        if isinstance(spec, dict):
            if "raw" in spec:
                return _sum_from_raw(dfx, spec["raw"], spec.get("types"))
            if "settlement_for" in spec:
                return _sum_from_raw(dfx, "settlement_amount",
                                     types=[spec["settlement_for"]])
            if spec.get("derived") == "smart_promotion":
                return _derive_smart_promotion(dfx)
            if spec.get("derived") == "fbt_free_shipping":
                return _derive_fbt_free_shipping(dfx)
        return {}

    rows: list[dict] = []

    # ── Quantity: total + per-product ──────────────────────────────
    rows.append({"Métrica": "━ Quantity ━"})
    qty_total = {"Métrica": "Quantity"}
    for m in all_months:
        qty_total[m] = int(units_total.get(m, 0))
    qty_total["Total"] = sum(qty_total[m] for m in all_months)
    rows.append(qty_total)
    if not units_per_product.empty:
        for prod, s in units_per_product.iterrows():
            r = {"Métrica": f"   {prod}"}
            for m in all_months:
                r[m] = int(s.get(m, 0) or 0)
            r["Total"] = sum(r[m] for m in all_months)
            rows.append(r)

    # ── P&L sections — every row pulls from raw directly (no SUM) ──
    for section_name, metrics in _PL_SECTIONS:
        rows.append({"Métrica": f"━ {section_name} ━"})
        for label, spec in metrics:
            by_month = _spec_values(spec)
            row = {"Métrica": label}
            for m in all_months:
                row[m] = round(float(by_month.get(m, 0.0)), 2)
            row["Total"] = round(sum(row[m] for m in all_months), 2)
            rows.append(row)

    # ── Unmapped fields — logged to session_state only (not rendered
    #    in the P&L to keep it clean). The caller can show them in an
    #    expander outside the P&L table.
    try:
        st.session_state["pl_unmapped"] = _unmapped_fields_sums(dfx)
    except Exception:
        pass

    pl = pd.DataFrame(rows).fillna("")
    return pl[["Métrica", *all_months, "Total"]]


def _render_pl_html(pl: pd.DataFrame) -> str:
    """Render the P&L DataFrame as a styled HTML table. Streamlit's
    st.dataframe strips most Styler CSS, so we build the HTML ourselves
    and inject it with st.markdown(unsafe_allow_html=True) for full
    control over row backgrounds and the dark/green header."""
    section_mask = pl["Métrica"].astype(str).str.startswith("━")
    # First row in each section is a TOTAL — bold + gray background.
    sum_labels   = {"Total settlement amount", "Net sales",
                    "Shipping", "Fees", "Adjustments", "Quantity"}
    sum_mask     = pl["Métrica"].astype(str).isin(sum_labels)

    headers = list(pl.columns)
    th = "".join(
        f'<th style="padding:8px 12px;text-align:{"left" if h=="Métrica" else "right"};">'
        f'{h}</th>' for h in headers
    )
    thead = (
        f'<thead><tr style="background:#000000;color:#00e676;'
        f'font-weight:700;border-bottom:2px solid #00e676;">{th}</tr></thead>'
    )

    body_rows = []
    in_quantity = False
    for i, row in pl.iterrows():
        label = str(row["Métrica"])
        if section_mask.loc[i]:
            in_quantity = (label == "━ Quantity ━")
            tr_style = ("background:#1f2937;color:#ffffff;"
                        "font-weight:700;")
        elif sum_mask.loc[i]:
            tr_style = "background:#e5e7eb;font-weight:700;"
        else:
            tr_style = ""

        cells = []
        for col in headers:
            v = row[col]
            if col == "Métrica":
                cells.append(
                    f'<td style="padding:6px 12px;text-align:left;">{label}</td>'
                )
                continue
            if v == "" or v is None:
                txt = ""
            else:
                try:
                    n = float(v)
                    # Integer formatting for Quantity section and the
                    # Quantity total row itself
                    if in_quantity or label == "Quantity":
                        txt = f"{int(n):,}"
                    else:
                        txt = f"€{n:,.2f}"
                except (TypeError, ValueError):
                    txt = str(v)
            cells.append(
                f'<td style="padding:6px 12px;text-align:right;">{txt}</td>'
            )
        body_rows.append(f'<tr style="{tr_style}">{"".join(cells)}</tr>')

    tbody = f'<tbody>{"".join(body_rows)}</tbody>'
    html = (
        '<div style="overflow-x:auto;border:1px solid #e5e7eb;'
        'border-radius:6px;">'
        '<table style="width:100%;border-collapse:collapse;'
        'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;'
        'font-size:13px;">'
        f'{thead}{tbody}</table></div>'
    )
    return html


def _load_order_items(order_ids: list[str]) -> pd.DataFrame:
    from backend.database import db_admin
    if not order_ids:
        return pd.DataFrame()
    # Supabase has a URL length limit — batch the IN query.
    rows: list[dict] = []
    batch_size = 300
    for i in range(0, len(order_ids), batch_size):
        chunk = order_ids[i:i + batch_size]
        got = (
            db_admin.table("tiktok_order_items")
            .select("order_id,sku_id,product_id,product_name,sku_name,seller_sku,quantity")
            .in_("order_id", chunk)
            .execute()
            .data
        ) or []
        rows.extend(got)
    return pd.DataFrame(rows)


def _load_cogs_map() -> dict[str, float]:
    """product_id (TikTok) → cogs_eur (via tiktok_product_map → products)."""
    from backend.database import db_admin
    map_rows = (
        db_admin.table("tiktok_product_map")
        .select("tiktok_product_id,asin").execute().data
    ) or []
    pid_to_asin = {r["tiktok_product_id"]: r["asin"] for r in map_rows}
    asins = list({v for v in pid_to_asin.values() if v})
    cogs_by_asin: dict[str, float] = {}
    for i in range(0, len(asins), 300):
        chunk = asins[i:i + 300]
        got = (
            db_admin.table("products")
            .select("asin,cogs_eur").in_("asin", chunk).execute().data
        ) or []
        for r in got:
            if r.get("asin") and r.get("cogs_eur") is not None:
                cogs_by_asin[r["asin"]] = float(r["cogs_eur"])
    return {pid: cogs_by_asin[asin]
            for pid, asin in pid_to_asin.items()
            if asin in cogs_by_asin}


def _render_per_product(fin_df: pd.DataFrame, start: date, end: date) -> None:
    if fin_df.empty:
        st.info("Sin datos. Haz Sync primero.")
        return

    order_ids = fin_df["order_adjustment_id"].dropna().astype(str).unique().tolist()
    items = _load_order_items(order_ids)
    if items.empty:
        st.warning(
            "No hay line-items para estos pedidos todavía. Haz click en "
            "**🔄 Sync from TikTok** para que corra también el enrichment."
        )
        return

    cogs_by_pid = _load_cogs_map()

    # Join: items ⟵ finance (one tx per order_id, settlement/net_sales there)
    fin_small = (
        fin_df[["order_adjustment_id", "statement_date", "net_sales",
                "total_settlement_amount"]]
        .rename(columns={"order_adjustment_id": "order_id"})
        .copy()
    )
    fin_small["order_id"] = fin_small["order_id"].astype(str)
    merged = items.merge(fin_small, on="order_id", how="left")
    merged["month"] = pd.to_datetime(merged["statement_date"]).dt.strftime("%Y-%m")
    merged["cogs_unit"] = merged["product_id"].map(cogs_by_pid).fillna(0.0)
    merged["cogs_total"] = merged["quantity"].fillna(0) * merged["cogs_unit"]

    # Allocate order-level net_sales / settlement pro-rata by quantity across
    # the order's SKUs (simple heuristic — fine when most orders are 1 SKU).
    qty_by_order = merged.groupby("order_id")["quantity"].transform("sum").replace(0, 1)
    merged["net_sales_alloc"]  = (
        pd.to_numeric(merged["net_sales"], errors="coerce").fillna(0)
        * merged["quantity"].fillna(0) / qty_by_order
    )
    merged["settlement_alloc"] = (
        pd.to_numeric(merged["total_settlement_amount"], errors="coerce").fillna(0)
        * merged["quantity"].fillna(0) / qty_by_order
    )
    merged["product_margin"]   = merged["settlement_alloc"] - merged["cogs_total"]

    label = merged["product_name"].fillna(merged["sku_name"]).fillna(merged["sku_id"])
    merged["product"] = label

    # Pivot: rows = product, columns = month, value = metric
    def _pivot(metric: str) -> pd.DataFrame:
        p = merged.pivot_table(index="product", columns="month",
                               values=metric, aggfunc="sum", fill_value=0)
        p["Total"] = p.sum(axis=1)
        p = p.reset_index().rename(columns={"product": "Producto"})
        return p

    st.caption(
        "Agregado por mes de statement_date. **Net sales / Settlement** se "
        "reparten por cantidad entre los SKUs de la orden. **COGS** viene de "
        "`products.cogs_eur` vía `tiktok_product_map`."
    )

    metric = st.radio(
        "Métrica",
        ["Unit Sales", "COGS (EUR)", "Net sales (EUR)", "Settlement (EUR)",
         "Product margin (EUR)"],
        horizontal=True, key="prod_metric",
    )
    metric_col = {
        "Unit Sales":              "quantity",
        "COGS (EUR)":              "cogs_total",
        "Net sales (EUR)":         "net_sales_alloc",
        "Settlement (EUR)":        "settlement_alloc",
        "Product margin (EUR)":    "product_margin",
    }[metric]

    pivot = _pivot(metric_col)
    st.dataframe(pivot, use_container_width=True, height=500, hide_index=True)

    buf = io.StringIO()
    pivot.to_csv(buf, index=False)
    st.download_button(
        f"⬇️ Export {metric} CSV",
        data=buf.getvalue().encode("utf-8-sig"),
        file_name=f"tiktok_per_product_{metric.replace(' ', '_')}_{start}_{end}.csv",
        mime="text/csv",
    )

    # Unmatched products warning
    unmatched = merged[merged["cogs_unit"] == 0]["product"].unique()
    if len(unmatched):
        st.warning(
            f"⚠️ {len(unmatched)} productos sin COGS en Supabase. Revisa que "
            f"`tiktok_product_map` tenga su product_id mapeado a un ASIN con "
            f"`products.cogs_eur`. Ejemplos: {list(unmatched)[:5]}"
        )


def render_finance_page():
    """Streamlit page: pulls TikTok finance transactions and exports to CSV."""
    st.markdown("#### 💰 Finance-TikTok")
    st.caption(
        "Transaction-level finance data from TikTok Shop (equivalent to the "
        "Order details sheet of the Finance Center Excel export)."
    )

    # ── Date range + Sync ──────────────────────────────────────
    today = date.today()
    default_start = today.replace(day=1) - pd.DateOffset(months=2)
    default_start = default_start.date()
    col_a, col_b, col_c = st.columns([2, 2, 1])
    with col_a:
        start = st.date_input("Desde", value=default_start, key="fin_start")
    with col_b:
        end = st.date_input("Hasta", value=today, key="fin_end")
    with col_c:
        st.write(" ")
        st.write(" ")
        sync_clicked = st.button("🔄 Sync from TikTok", type="primary",
                                 use_container_width=True)

    if sync_clicked:
        days = max((today - start).days + 1, 30)
        progress = st.empty()
        warnings: list[str] = []

        progress.info("⏳ Paso 1/2: Fetching finance statements + transactions + payments… (puede tomar 1-3 min)")
        try:
            from backend.fetchers.tiktok_finance import fetch_tiktok_finance
            n_rows, w1 = fetch_tiktok_finance(days_back=days)
            warnings += w1
        except Exception as exc:
            n_rows = 0
            warnings.append(f"Finance fetch failed: {exc}")

        progress.info("⏳ Paso 2/2: Enriqueciendo órdenes con SKU/producto/quantity…")
        try:
            from backend.fetchers.tiktok_orders_enrich import enrich_tiktok_orders
            n_items, w2 = enrich_tiktok_orders(days_back=days)
            warnings += w2
        except Exception as exc:
            n_items = 0
            warnings.append(f"Order enrichment skipped: {exc}")

        progress.empty()
        if n_rows:
            st.success(f"✅ {n_rows} transacciones · {n_items} line-items actualizados.")
        else:
            st.warning("No se actualizaron filas — revisa los mensajes abajo.")
        with st.expander("📋 Detalles de la sincronización (clickéame)",
                         expanded=True):
            for w in warnings:
                st.text(w)

    df = _load_transactions(start, end)
    display_df = _order_details_df(df)

    k1, k2, k3 = st.columns(3)
    with k1:
        st.metric("Transacciones", len(display_df))
    with k2:
        total = pd.to_numeric(
            display_df["Total settlement amount"], errors="coerce"
        ).sum() if "Total settlement amount" in display_df else 0
        st.metric("Total settlement (EUR)", f"{total:,.2f}")
    with k3:
        net = pd.to_numeric(
            display_df["Net sales"], errors="coerce"
        ).sum() if "Net sales" in display_df else 0
        st.metric("Net sales (EUR)", f"{net:,.2f}")

    if display_df.empty:
        st.info(
            "Sin datos para el rango seleccionado. Haz click en **🔄 Sync from TikTok** "
            "para traerlos del API, o verifica el rango de fechas."
        )
        return

    # ── Three tabs: P&L, por producto, tabla cruda ─────────────
    tab_pl, tab_prod, tab_raw = st.tabs([
        "📊 P&L por mes",
        "📦 Por producto",
        "📋 Tabla cruda (Order details)",
    ])

    with tab_pl:
        month_source_label = st.radio(
            "Agrupar por mes de:",
            ["Statement date", "Order date"],
            horizontal=True, key="pl_month_source",
        )
        month_source = ("statement_date"
                        if month_source_label == "Statement date"
                        else "order_created_date")
        units_total, units_per_product = _units_by_month(df, month_source)
        pl = _pl_pivot(df, units_total, units_per_product, month_source)
        if pl.empty:
            st.info("Sin datos para pivotar.")
        else:
            st.caption(
                f"Agregado por **{month_source_label.lower()}**. "
                "**COGS no está aquí** — está en el tab 📦 Por producto."
            )
            if sum(units_total.values()) == 0:
                st.warning(
                    "⚠️ Quantity = 0 en todos los meses. Significa que la "
                    "tabla `tiktok_order_items` está vacía. Revisa que "
                    "corriste la migración `003_tiktok_order_items.sql` en "
                    "Supabase y que el último Sync mostró 'N line-items "
                    "actualizados'."
                )
            st.markdown(_render_pl_html(pl), unsafe_allow_html=True)
            # Advanced: show unmapped fields in a collapsed expander so new
            # TikTok fields never silently miss, but the P&L stays clean.
            unmapped = st.session_state.get("pl_unmapped") or {}
            if unmapped:
                with st.expander(
                    f"🔍 Campos del raw sin mapear al P&L ({len(unmapped)})",
                    expanded=False,
                ):
                    st.caption(
                        "Estos campos vienen en el raw del API pero no están "
                        "en ninguna fila del P&L. Si alguno es relevante, "
                        "dilo y lo agrego a la sección correcta."
                    )
                    rows_un = []
                    for field, sums in unmapped.items():
                        r = {"Campo (raw)": field,
                             "Total": round(sum(sums.values()), 2)}
                        for m, v in sums.items():
                            r[m] = round(v, 2)
                        rows_un.append(r)
                    st.dataframe(pd.DataFrame(rows_un),
                                 use_container_width=True, hide_index=True)
            buf_pl = io.StringIO()
            pl.to_csv(buf_pl, index=False)
            st.download_button(
                "⬇️ Export P&L CSV",
                data=buf_pl.getvalue().encode("utf-8-sig"),
                file_name=f"tiktok_pl_{start}_{end}.csv",
                mime="text/csv",
            )

    with tab_prod:
        _render_per_product(df, start, end)

    with tab_raw:
        # CSV export (same columns as Order details) for pasting into Sheet
        buf = io.StringIO()
        display_df.to_csv(buf, index=False)
        st.download_button(
            "⬇️ Export CSV (Order details format)",
            data=buf.getvalue().encode("utf-8-sig"),
            file_name=f"tiktok_order_details_{start}_{end}.csv",
            mime="text/csv",
        )
        st.dataframe(display_df, use_container_width=True, height=600, hide_index=True)
