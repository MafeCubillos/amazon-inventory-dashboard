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
    from backend.database import db_admin
    rows = (
        db_admin.table("tiktok_finance_transactions")
        .select("*")
        .gte("statement_date", start.isoformat())
        .lte("statement_date", end.isoformat())
        .order("statement_date", desc=True)
        .limit(10000)
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


# ── P&L row structure (metric label, db columns to sum, sign) ────────
# Mirrors the P&L / P&LBase tabs of the Google Sheet. Only rows derivable
# from Finance data are here; COGS and Ads come from other sources.
_PL_SECTIONS: list[tuple[str, list[tuple[str, list[str], int]]]] = [
    ("GMV / Ventas", [
        ("GMV incl VAT",                            ["subtotal_before_discounts"],              +1),
        ("Seller discounts",                        ["seller_discounts"],                       +1),
        ("Refund subtotal before seller discounts", ["refund_subtotal_before_seller_discounts"],+1),
        ("Refund of seller discounts",              ["refund_of_seller_discounts"],             +1),
        ("Net sales",                               ["net_sales"],                              +1),
        ("Customer payment",                        ["customer_payment"],                       +1),
        ("Customer refund",                         ["customer_refund"],                        +1),
    ]),
    ("Shipping", [
        ("TikTok Shop shipping fee",                ["tiktok_shop_shipping_fee"],               +1),
        ("Fulfilled by TikTok Shop shipping fee",   ["fulfilled_by_tiktok_shop_shipping_fee"],  +1),
        ("Customer shipping fee",                   ["customer_shipping_fee"],                  +1),
        ("Refunded customer shipping fee",          ["refunded_customer_shipping_fee"],         +1),
        ("TikTok Shop shipping incentive",          ["tiktok_shop_shipping_incentive"],         +1),
        ("Shipping subsidy",                        ["shipping_subsidy"],                       +1),
        ("Actual return shipping fee",              ["actual_return_shipping_fee"],             +1),
        ("Shipping fee discount",                   ["shipping_fee_discount"],                  +1),
        ("Fee for FBT free shipping",               ["fee_for_fbt_free_shipping"],              +1),
        ("Shipping (total)",                        ["shipping"],                               +1),
    ]),
    ("Fees", [
        ("TikTok Shop commission fee",              ["tiktok_shop_commission_fee"],             +1),
        ("Affiliate commission",                    ["affiliate_commission"],                   +1),
        ("Affiliate partner commission",            ["affiliate_partner_commission"],           +1),
        ("Transaction / Campaign service fee",      ["campaign_service_fee"],                   +1),
        ("Tax and duty / VAT",                      ["tax_and_duty"],                           +1),
        ("Fees (total)",                            ["fees"],                                   +1),
    ]),
    ("Advertising", [
        ("GMV Payment for TikTok Ads",              ["affiliate_shop_ads_commission"],          +1),
    ]),
    ("Resultado", [
        ("Adjustments",                             ["adjustment_amount"],                      +1),
        ("Total settlement",                        ["total_settlement_amount"],                +1),
    ]),
]


def _pl_pivot(df: pd.DataFrame) -> pd.DataFrame:
    """Build a P&L-style DataFrame: rows = metrics, columns = months."""
    if df.empty or "statement_date" not in df.columns:
        return pd.DataFrame()
    dfx = df.copy()
    dfx["month"] = pd.to_datetime(dfx["statement_date"]).dt.strftime("%Y-%m")
    rows: list[dict] = []
    for section_name, metrics in _PL_SECTIONS:
        rows.append({"Métrica": f"━ {section_name} ━"})  # section header
        for label, cols, sign in metrics:
            present = [c for c in cols if c in dfx.columns]
            if not present:
                continue
            series = pd.to_numeric(dfx[present[0]], errors="coerce").fillna(0) * sign
            by_month = series.groupby(dfx["month"]).sum()
            row = {"Métrica": label}
            for m, v in by_month.items():
                row[m] = round(float(v), 2)
            row["Total"] = round(float(by_month.sum()), 2)
            rows.append(row)
    pl = pd.DataFrame(rows).fillna("")
    months = sorted([c for c in pl.columns if c not in ("Métrica", "Total")])
    return pl[["Métrica", *months, "Total"]]


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
        with st.spinner("Fetching from TikTok Finance API…"):
            from backend.fetchers.tiktok_finance import fetch_tiktok_finance
            days = max((today - start).days + 1, 30)
            n_rows, warnings = fetch_tiktok_finance(days_back=days)
        if n_rows:
            st.success(f"✅ {n_rows} transacciones actualizadas.")
        else:
            st.warning("No se actualizaron filas — revisa los mensajes abajo.")
        with st.expander("Detalles de la sincronización", expanded=not n_rows):
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

    # ── Two tabs: P&L view and raw table ───────────────────────
    tab_pl, tab_raw = st.tabs(["📊 P&L por mes", "📋 Tabla cruda (Order details)"])

    with tab_pl:
        pl = _pl_pivot(df)
        if pl.empty:
            st.info("Sin datos para pivotar.")
        else:
            st.caption(
                "Agregado por mes de statement_date. **COGS, Ads y Advertising cost "
                "no están aquí** — vienen de otras fuentes y los sumas en el Sheet."
            )
            st.dataframe(pl, use_container_width=True, height=700, hide_index=True)
            # CSV export of P&L
            buf_pl = io.StringIO()
            pl.to_csv(buf_pl, index=False)
            st.download_button(
                "⬇️ Export P&L CSV",
                data=buf_pl.getvalue().encode("utf-8-sig"),
                file_name=f"tiktok_pl_{start}_{end}.csv",
                mime="text/csv",
            )

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
