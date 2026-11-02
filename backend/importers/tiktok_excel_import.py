"""Import TikTok Finance Center → Statements Excel into
tiktok_finance_transactions. Backfills row types that the API's
/finance/202309/statements endpoint doesn't surface (Top up ads
balance, Bill deduction, Bill payment).

Reuses the same natural key as the API fetcher
(statement_id, order_adjustment_id, type) so uploading the same
Excel twice simply overwrites (idempotent).

The Excel's "Order details" sheet has 70 columns. We map a minimal
subset into both the typed columns and the `raw` JSON so the existing
P&L reads them without changes.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timezone

from openpyxl import load_workbook

from backend.database import db_admin
from backend.tiktok_tokens import get_tokens

logger = logging.getLogger(__name__)


def _norm_type(label: str) -> str:
    """'Top up ads balance using GMV payments' → 'TOP_UP_ADS_BALANCE_USING_GMV_PAYMENTS'"""
    if not label:
        return "UNKNOWN"
    s = str(label).strip()
    s = re.sub(r"\s*[（(][^）)]*[）)]", "", s)  # strip (...) and （...）
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").upper()
    return s or "UNKNOWN"


def _dec(v):
    if v is None or v == "" or v == "/":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    if v is None or v == "" or v == "/":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _str(v):
    if v is None or v == "":
        return None
    s = str(v).strip()
    return s or None


def _parse_date(v):
    """TikTok Excel dates come as 'YYYY/MM/DD' strings."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return None


# Excel-column → our DB-column mapping (only the ones the P&L reads).
# Columns we skip just land in `raw` under the Excel name.
_EXCEL_TO_DB = {
    "Statement date":                               "statement_date",
    "Statement ID":                                 "statement_id",
    "Payment ID":                                   "payment_id",
    "Status":                                       "status",
    "Currency":                                     "currency",
    "Type":                                         "type",
    "Order/adjustment ID":                          "order_adjustment_id",
    "SKU ID":                                       "sku_id",
    "Quantity":                                     "quantity",
    "Product name":                                 "product_name",
    "SKU name":                                     "sku_name",
    "Order created date":                           "order_created_date",
    "Order delivery date":                          "order_delivery_date",
    "Total settlement amount":                      "total_settlement_amount",
    "Net sales":                                    "net_sales",
    "Subtotal before discounts":                    "subtotal_before_discounts",
    "Seller discounts":                             "seller_discounts",
    "Refund subtotal before seller discounts":      "refund_subtotal_before_seller_discounts",
    "Refund of seller discounts":                   "refund_of_seller_discounts",
    "Shipping":                                     "shipping",
    "Fulfilled by TikTok Shop shipping fee":        "fulfilled_by_tiktok_shop_shipping_fee",
    "Customer shipping fee":                        "customer_shipping_fee",
    "Refunded customer shipping fee":               "refunded_customer_shipping_fee",
    "TikTok Shop shipping incentive":               "tiktok_shop_shipping_incentive",
    "TikTok Shop shipping incentive refund":        "tiktok_shop_shipping_incentive_refund",
    "Shipping subsidy":                             "shipping_subsidy",
    "Actual return shipping fee":                   "actual_return_shipping_fee",
    "Fee for FBT free shipping":                    "fee_for_fbt_free_shipping",
    "Fees":                                         "fees",
    "TikTok Shop commission fee":                   "tiktok_shop_commission_fee",
    "Affiliate Commission":                         "affiliate_commission",
    "Smart Promotion fee":                          "smart_promotion_fee",
    "Adjustment amount":                            "adjustment_amount",
    "Adjustment reason":                            "adjustment_reason",
    "Related order ID":                             "related_order_id",
}

# Fields the P&L reads from raw JSON — we mirror the Excel values into
# these raw keys so the pivot picks them up without extra mapping.
_EXCEL_TO_RAW = {
    "Total settlement amount":                      "settlement_amount",
    "Net sales":                                    "net_sales_amount",
    "Subtotal before discounts":                    "gross_sales_amount",
    "Seller discounts":                             "seller_discount_amount",
    "Refund subtotal before seller discounts":      "gross_sales_refund_amount",
    "Refund of seller discounts":                   "seller_discount_refund_amount",
    "Shipping":                                     "shipping_cost_amount",
    "Fulfilled by TikTok Shop shipping fee":        "fbt_shipping_cost_amount",
    "Customer shipping fee":                        "customer_shipping_fee_amount",
    "Refunded customer shipping fee":               "customer_paid_shipping_fee_refund_amount",
    "TikTok Shop shipping incentive":               "shipping_cost_discount_amount",
    "TikTok Shop shipping incentive refund":        "refund_shipping_cost_discount_amount",
    "Shipping subsidy":                             "shipping_fee_subsidy_amount",
    "Actual return shipping fee":                   "actual_return_shipping_fee_amount",
    "Fees":                                         "fee_amount",
    "TikTok Shop commission fee":                   "platform_commission_amount",
    "Affiliate Commission":                         "affiliate_commission_amount",
    "Affiliate partner commission":                 "affiliate_partner_commission_amount",
    "Affiliate Shop Ads commission":                "affiliate_ads_commission_amount",
}

_DEC_COLUMNS = {
    "total_settlement_amount", "net_sales", "subtotal_before_discounts",
    "seller_discounts", "refund_subtotal_before_seller_discounts",
    "refund_of_seller_discounts", "shipping",
    "fulfilled_by_tiktok_shop_shipping_fee", "customer_shipping_fee",
    "refunded_customer_shipping_fee", "tiktok_shop_shipping_incentive",
    "tiktok_shop_shipping_incentive_refund", "shipping_subsidy",
    "actual_return_shipping_fee", "fee_for_fbt_free_shipping", "fees",
    "tiktok_shop_commission_fee", "affiliate_commission",
    "smart_promotion_fee", "adjustment_amount",
}


def import_tiktok_excel(file_bytes_or_io) -> tuple[int, list[str]]:
    """Parse the uploaded TikTok Excel's 'Order details' sheet and upsert
    every row into tiktok_finance_transactions. Returns (n_rows, warnings)."""
    warnings: list[str] = []
    # Streamlit's UploadedFile is io.BytesIO-compatible
    if hasattr(file_bytes_or_io, "read"):
        content = file_bytes_or_io.read()
    else:
        content = file_bytes_or_io
    wb = load_workbook(io.BytesIO(content), data_only=True)

    if "Order details" not in wb.sheetnames:
        return 0, ["Excel sin pestaña 'Order details' — nada que importar."]

    ws = wb["Order details"]
    hdrs = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    warnings.append(f"Hojas: {', '.join(wb.sheetnames)}. "
                    f"Order details: {len(hdrs)} columnas, {len(rows)} filas.")

    tokens = get_tokens() or {}
    shop_cipher = tokens.get("shop_cipher") or "manual-upload"

    # ── Figure out which months this Excel covers so we can wipe
    #    pre-existing rows for those months first. Guarantees the
    #    Excel's data wins and never leaves duplicates/stale rows. ──
    stmt_col = hdrs.index("Statement date") if "Statement date" in hdrs else None
    months_in_file: set[str] = set()
    if stmt_col is not None:
        for r in rows:
            iso = _parse_date(r[stmt_col])
            if iso:
                months_in_file.add(iso[:7])  # YYYY-MM

    if months_in_file:
        for ym in sorted(months_in_file):
            start = f"{ym}-01"
            # end = next month's day 01 (exclusive)
            y, m = ym.split("-")
            next_m = int(m) + 1
            next_y = int(y) + (1 if next_m > 12 else 0)
            next_m = 1 if next_m > 12 else next_m
            end = f"{next_y:04d}-{next_m:02d}-01"
            try:
                deleted = (db_admin.table("tiktok_finance_transactions")
                           .delete()
                           .gte("statement_date", start)
                           .lt("statement_date", end)
                           .execute())
                n = len(deleted.data or [])
                warnings.append(f"Wiped {n} existing rows for {ym} "
                                f"(Excel is authoritative).")
            except Exception as exc:
                warnings.append(f"Could not wipe {ym}: {exc}")

    upserted = 0
    skipped = 0
    types_seen: dict[str, int] = {}
    for r in rows:
        rowd = dict(zip(hdrs, r))
        stmt_id = _str(rowd.get("Statement ID"))
        adj_id  = _str(rowd.get("Order/adjustment ID"))
        if not stmt_id or not adj_id:
            skipped += 1
            continue
        tx_type_label = rowd.get("Type")
        tx_type = _norm_type(tx_type_label)
        types_seen[tx_type] = types_seen.get(tx_type, 0) + 1

        # Build DB row (typed columns)
        db_row: dict = {
            "statement_id":        stmt_id,
            "order_adjustment_id": adj_id,
            "type":                tx_type,
            "shop_cipher":         shop_cipher,
            "fetched_at":          datetime.now(timezone.utc).isoformat(),
        }
        for excel_col, db_col in _EXCEL_TO_DB.items():
            if excel_col not in rowd:
                continue
            v = rowd[excel_col]
            if db_col in ("statement_date", "order_created_date", "order_delivery_date"):
                db_row[db_col] = _parse_date(v)
            elif db_col == "quantity":
                db_row[db_col] = _int(v)
            elif db_col in _DEC_COLUMNS:
                db_row[db_col] = _dec(v)
            else:
                db_row[db_col] = _str(v) if v is not None else None
        # Type gets normalized value
        db_row["type"] = tx_type

        # Build raw JSON with API-ish field names (so P&L reads it)
        raw: dict = {"type": tx_type}
        for excel_col, raw_key in _EXCEL_TO_RAW.items():
            if excel_col not in rowd:
                continue
            v = _dec(rowd[excel_col])
            raw[raw_key] = str(v) if v is not None else "0"
        # Keep every other Excel value too, under the Excel column name
        for k, v in rowd.items():
            if k and k not in _EXCEL_TO_DB and k not in _EXCEL_TO_RAW:
                raw[k] = v if (v is None or isinstance(v, (int, float, str, bool))) else str(v)
        db_row["raw"] = raw

        try:
            db_admin.table("tiktok_finance_transactions").upsert(
                db_row, on_conflict="statement_id,order_adjustment_id,type"
            ).execute()
            upserted += 1
        except Exception as exc:
            warnings.append(f"Save failed stmt={stmt_id} adj={adj_id}: {exc}")

    warnings.append(f"Upserted: {upserted}, skipped (no IDs): {skipped}.")
    warnings.append("Tipos vistos: " + ", ".join(
        f"{k}({v})" for k, v in sorted(types_seen.items(), key=lambda x: -x[1])))

    # Mark months as "closed" so Sync doesn't overwrite them
    for ym in sorted(months_in_file):
        try:
            db_admin.table("tiktok_finance_closed_months").upsert({
                "month":  ym,
                "source": "excel",
            }, on_conflict="month").execute()
        except Exception as exc:
            warnings.append(f"Could not mark {ym} as closed: {exc}")
    if months_in_file:
        warnings.append(f"✅ Meses cerrados: {', '.join(sorted(months_in_file))} "
                        f"(Sync no los sobrescribirá).")

    return upserted, warnings
