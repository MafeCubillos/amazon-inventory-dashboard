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


def render_finance_page():
    """Streamlit page: pulls TikTok finance transactions and exports to CSV."""
    st.markdown("#### 💰 TikTok Finance")
    st.caption(
        "Transaction-level finance data from TikTok Shop (equivalent to the "
        "Order details sheet of the Finance Center Excel export). Use **Export CSV** "
        "to paste directly into your P&L Google Sheet."
    )

    # ── Date range ──────────────────────────────────────────────
    today = date.today()
    default_start = today.replace(day=1)
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

    # ── Sync ────────────────────────────────────────────────────
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

    # ── Load + display ──────────────────────────────────────────
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

    # ── CSV export (same columns as Order details) ──────────────
    buf = io.StringIO()
    display_df.to_csv(buf, index=False)
    st.download_button(
        "⬇️ Export CSV (Order details format)",
        data=buf.getvalue().encode("utf-8-sig"),
        file_name=f"tiktok_order_details_{start}_{end}.csv",
        mime="text/csv",
    )

    # ── Preview table ───────────────────────────────────────────
    st.dataframe(display_df, use_container_width=True, height=600, hide_index=True)
