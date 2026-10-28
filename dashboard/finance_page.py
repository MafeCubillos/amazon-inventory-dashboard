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
    """Load finance rows whose ORDER was placed in [start, end].
    Also includes rows with no order_created_date (adjustments) whose
    statement_date falls in the window — so numbers don't disappear."""
    from backend.database import db_admin
    s, e = start.isoformat(), end.isoformat()
    # Supabase .or_() = OR of filters; we want:
    #   order_created_date BETWEEN s AND e
    #   OR (order_created_date IS NULL AND statement_date BETWEEN s AND e)
    rows = (
        db_admin.table("tiktok_finance_transactions")
        .select("*")
        .or_(
            f"and(order_created_date.gte.{s},order_created_date.lte.{e}),"
            f"and(order_created_date.is.null,statement_date.gte.{s},statement_date.lte.{e})"
        )
        .order("order_created_date", desc=True)
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


# ── P&L row structure (metric label, db columns to sum) ────────────
# Each section has detail rows and ends with a computed total row.
# Mirrors the P&L / P&LBase tabs of the Google Sheet.
# Rows marked SUM = sum of the detail rows above (computed, not from a column).
_PL_SECTIONS: list[tuple[str, list[tuple[str, list[str] | str]]]] = [
    ("Ventas", [
        ("GMV incl VAT",                            ["subtotal_before_discounts"]),
        ("Seller discounts",                        ["seller_discounts"]),
        ("Refund subtotal before seller discounts", ["refund_subtotal_before_seller_discounts"]),
        ("Refund of seller discounts",              ["refund_of_seller_discounts"]),
        ("Net sales",                               ["net_sales"]),
        ("Customer payment",                        ["customer_payment"]),
        ("Customer refund",                         ["customer_refund"]),
    ]),
    ("Shipping", [
        ("TikTok Shop shipping fee",                ["tiktok_shop_shipping_fee"]),
        ("Fulfilled by TikTok Shop shipping fee",   ["fulfilled_by_tiktok_shop_shipping_fee"]),
        ("Customer shipping fee",                   ["customer_shipping_fee"]),
        ("Refunded customer shipping fee",          ["refunded_customer_shipping_fee"]),
        ("TikTok Shop shipping incentive",          ["tiktok_shop_shipping_incentive"]),
        ("Shipping subsidy",                        ["shipping_subsidy"]),
        ("Actual return shipping fee",              ["actual_return_shipping_fee"]),
        ("Shipping fee discount",                   ["shipping_fee_discount"]),
        ("Fee for FBT free shipping",               ["fee_for_fbt_free_shipping"]),
        ("Shipping (total)",                        "SUM"),
    ]),
    ("Fees", [
        ("TikTok Shop commission fee",              ["tiktok_shop_commission_fee"]),
        ("Affiliate commission",                    ["affiliate_commission"]),
        ("Affiliate partner commission",            ["affiliate_partner_commission"]),
        ("Transaction / Campaign service fee",      ["campaign_service_fee"]),
        ("Tax and duty / VAT",                      ["tax_and_duty"]),
        ("Fees (total)",                            "SUM"),
    ]),
    ("Advertising", [
        ("GMV Payment for TikTok Ads",              ["affiliate_shop_ads_commission"]),
        ("Advertising (total)",                     "SUM"),
    ]),
    ("Resultado", [
        ("Adjustments",                             ["adjustment_amount"]),
        ("Total settlement",                        ["total_settlement_amount"]),
    ]),
]


def _units_by_month(fin_df: pd.DataFrame) -> dict[str, int]:
    """Return {YYYY-MM: unit count} from tiktok_order_items joined to the
    orders currently loaded in fin_df (by order_created_date)."""
    if fin_df.empty:
        return {}
    order_ids = fin_df["order_adjustment_id"].dropna().astype(str).unique().tolist()
    items = _load_order_items(order_ids)
    if items.empty:
        return {}
    # month comes from the finance row's order_created_date
    fin_small = (
        fin_df[["order_adjustment_id", "order_created_date"]]
        .rename(columns={"order_adjustment_id": "order_id"})
        .copy()
    )
    fin_small["order_id"] = fin_small["order_id"].astype(str)
    merged = items.merge(fin_small, on="order_id", how="left")
    merged["month"] = pd.to_datetime(merged["order_created_date"]).dt.strftime("%Y-%m")
    return (
        merged.dropna(subset=["month"])
              .groupby("month")["quantity"].sum().astype(int).to_dict()
    )


def _pl_pivot(df: pd.DataFrame, units: dict[str, int]) -> pd.DataFrame:
    """Build a P&L DataFrame: rows = metrics, columns = months (by
    order_created_date — that's how the Google Sheet reports it)."""
    if df.empty or "order_created_date" not in df.columns:
        return pd.DataFrame()
    dfx = df.copy()
    dfx["month"] = pd.to_datetime(dfx["order_created_date"]).dt.strftime("%Y-%m")
    all_months = sorted(dfx["month"].dropna().unique().tolist())

    rows: list[dict] = []

    # ── Unit sales (top row, from order_items) ─────────────────────
    unit_row = {"Métrica": "Unidades vendidas"}
    for m in all_months:
        unit_row[m] = int(units.get(m, 0))
    unit_row["Total"] = sum(unit_row[m] for m in all_months)
    rows.append(unit_row)

    for section_name, metrics in _PL_SECTIONS:
        rows.append({"Métrica": f"━ {section_name} ━"})
        section_detail_rows: list[dict] = []
        for label, cols in metrics:
            if cols == "SUM":
                # Sum all preceding detail rows IN THIS SECTION
                summed = {"Métrica": label}
                for m in all_months:
                    summed[m] = round(sum(r.get(m, 0) or 0 for r in section_detail_rows), 2)
                summed["Total"] = round(sum(summed[m] for m in all_months), 2)
                rows.append(summed)
                continue
            present = [c for c in cols if c in dfx.columns]
            if not present:
                continue
            series = pd.to_numeric(dfx[present[0]], errors="coerce").fillna(0)
            by_month = series.groupby(dfx["month"]).sum().to_dict()
            row = {"Métrica": label}
            for m in all_months:
                row[m] = round(float(by_month.get(m, 0.0)), 2)
            row["Total"] = round(sum(row[m] for m in all_months), 2)
            rows.append(row)
            section_detail_rows.append(row)

    pl = pd.DataFrame(rows).fillna("")
    return pl[["Métrica", *all_months, "Total"]]


def _style_pl(pl: pd.DataFrame):
    """Formatting: EUR symbol, bold section headers (dark), SUM rows (gray),
    dark table header. Unidades vendidas renders as integer, not EUR."""
    section_mask = pl["Métrica"].astype(str).str.startswith("━")
    total_labels = {"Shipping (total)", "Fees (total)", "Advertising (total)",
                    "Total settlement", "Unidades vendidas"}
    sum_mask     = pl["Métrica"].isin(total_labels)

    def _row_style(row):
        if section_mask.loc[row.name]:
            return ["background-color: #1f2937; color: #ffffff; font-weight: 700;"
                    for _ in row]
        if sum_mask.loc[row.name]:
            return ["background-color: #f3f4f6; font-weight: 700;" for _ in row]
        return ["" for _ in row]

    money_cols = [c for c in pl.columns if c != "Métrica"]
    units_rows = pl.index[pl["Métrica"] == "Unidades vendidas"].tolist()
    money_rows = [i for i in pl.index if i not in units_rows]

    def _fmt_eur(v):
        if v == "" or v is None:
            return ""
        try:
            return f"€{float(v):,.2f}"
        except (TypeError, ValueError):
            return v

    def _fmt_int(v):
        if v == "" or v is None:
            return ""
        try:
            return f"{int(float(v)):,}"
        except (TypeError, ValueError):
            return v

    styled = (
        pl.style
          .apply(_row_style, axis=1)
          .format(_fmt_eur, subset=(money_rows, money_cols))
          .format(_fmt_int, subset=(units_rows, money_cols))
          .set_table_styles([
              {"selector": "thead tr th",
               "props": "background-color: #111827; color: #ffffff; "
                        "font-weight: 700; text-align: right;"},
              {"selector": "thead tr th:first-child",
               "props": "text-align: left;"},
              {"selector": "tbody tr td",
               "props": "text-align: right;"},
              {"selector": "tbody tr td:first-child",
               "props": "text-align: left;"},
          ])
    )
    return styled


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
        with st.spinner("Fetching from TikTok Finance API…"):
            from backend.fetchers.tiktok_finance import fetch_tiktok_finance
            n_rows, warnings = fetch_tiktok_finance(days_back=days)
        with st.spinner("Enriqueciendo con SKU/producto desde Order API…"):
            try:
                from backend.fetchers.tiktok_orders_enrich import enrich_tiktok_orders
                n_items, w2 = enrich_tiktok_orders(days_back=days)
                warnings += w2
            except Exception as exc:
                n_items = 0
                warnings.append(f"Order enrichment skipped: {exc}")
        if n_rows:
            st.success(f"✅ {n_rows} transacciones · {n_items} line-items actualizados.")
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

    # ── Three tabs: P&L, por producto, tabla cruda ─────────────
    tab_pl, tab_prod, tab_raw = st.tabs([
        "📊 P&L por mes",
        "📦 Por producto",
        "📋 Tabla cruda (Order details)",
    ])

    with tab_pl:
        units = _units_by_month(df)
        pl = _pl_pivot(df, units)
        if pl.empty:
            st.info("Sin datos para pivotar.")
        else:
            st.caption(
                "Agregado por **mes de la orden** (order_created_date). "
                "**COGS no está aquí** — está en el tab 📦 Por producto. "
                "**Ads externo** (gasto en TikTok Ads Manager) tampoco."
            )
            st.dataframe(_style_pl(pl), use_container_width=True, height=700,
                         hide_index=True)
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
