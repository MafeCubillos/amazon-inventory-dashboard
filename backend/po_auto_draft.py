"""Auto-draft Purchase Orders from reorder suggestions.

Flow:
  1. Load every reorder_alerts row with suggested_reorder_qty > 0.
  2. Group suggestions by catalog product via each product's `asins` list.
  3. Sum quantities across marketplaces (so a product selling in 4 EU
     stores aggregates into one planned PO).
  4. If the summed quantity ≥ threshold × MOQ, round up to the next MOQ
     multiple and create a draft PO — unless an open draft already
     exists for the same supplier + product name (dedup by first line
     item description).
  5. Return the list of newly created drafts so the caller can email
     María a summary.

Threshold is 0.70 by default (María's rule: ≥70% of MOQ warrants an
order now, under that we wait).
"""
from __future__ import annotations

import math
from collections import defaultdict

from backend.database import db_admin
from backend.purchase_orders import create_purchase_order, get_purchase_order


def _load_catalog_products() -> list[dict]:
    rows = (db_admin.table("po_products")
            .select("id,name,description,unit_price,moq,"
                    "default_supplier_id,asins,active")
            .eq("active", True)
            .not_.is_("default_supplier_id", "null")
            .execute().data) or []
    return rows


def _load_open_drafts_product_names() -> set[tuple[int, str]]:
    """Set of (supplier_id, first-line-of-description) for every open
    draft so we don't duplicate an already-pending PO."""
    rows = (db_admin.table("po_orders")
            .select("id,supplier_id,status,po_order_items(description)")
            .in_("status", ["draft", "sent"])
            .execute().data) or []
    out: set[tuple[int, str]] = set()
    for po in rows:
        sid = po.get("supplier_id")
        for it in (po.get("po_order_items") or []):
            first = ((it.get("description") or "").splitlines() or [""])[0].strip()
            if first:
                out.add((sid, first))
                break
    return out


def _draft_note(suggested: int, breakdown: list[dict],
                final_qty: int, moq: int) -> str:
    parts = ", ".join(f"{b['marketplace']}:{b['qty']}" for b in breakdown)
    return (f"Auto-generado: suma Amazon = {suggested} ud ({parts}). "
            f"Redondeado a {final_qty} (MOQ {moq}).")


def _load_reorder_suggestions() -> list[dict]:
    rows = (db_admin.table("reorder_alerts")
            .select("asin,marketplace,suggested_reorder_qty")
            .gt("suggested_reorder_qty", 0)
            .execute().data) or []
    return rows


def scan_and_create_drafts(threshold_pct: float = 0.70) -> list[dict]:
    """Returns a list of {po, product, suggested_qty, final_qty, breakdown}
    for every draft newly created in this scan."""
    products = _load_catalog_products()
    if not products:
        return []

    # Build asin → product lookup (dup ASINs mean last-wins; fine)
    asin_to_product: dict[str, dict] = {}
    for p in products:
        for a in (p.get("asins") or []):
            if a and a.strip():
                asin_to_product[a.strip().upper()] = p

    alerts = _load_reorder_suggestions()

    # Aggregate per product
    buckets: dict[int, dict] = defaultdict(
        lambda: {"product": None, "qty": 0, "breakdown": []})
    for a in alerts:
        asin = (a.get("asin") or "").strip().upper()
        p    = asin_to_product.get(asin)
        if not p:
            continue
        b = buckets[p["id"]]
        b["product"] = p
        b["qty"]    += int(a.get("suggested_reorder_qty") or 0)
        b["breakdown"].append(
            {"asin": asin, "marketplace": a.get("marketplace"),
             "qty": int(a.get("suggested_reorder_qty") or 0)})

    existing = _load_open_drafts_product_names()
    created: list[dict] = []

    for bundle in buckets.values():
        p   = bundle["product"]
        qty = bundle["qty"]
        moq = float(p["moq"] or 0)
        if moq <= 0 or qty < threshold_pct * moq:
            continue
        # Dedup: skip if a draft/sent PO already exists for this product
        dedup_key = (p["default_supplier_id"],
                     ((p.get("description") or p.get("name") or "")
                       .splitlines() or [""])[0].strip())
        if dedup_key in existing:
            continue

        final_qty = math.ceil(qty / moq) * moq
        po = create_purchase_order(
            supplier_id = p["default_supplier_id"],
            items       = [{
                "description": p["description"],
                "quantity":    final_qty,
                "unit_price":  float(p["unit_price"] or 0),
            }],
            notes       = _draft_note(qty, bundle["breakdown"],
                                       int(final_qty), int(moq)),
        )
        created.append({
            "po":            po,
            "product":       p,
            "suggested_qty": qty,
            "final_qty":     int(final_qty),
            "breakdown":     bundle["breakdown"],
        })

    return created


# ─────────────────────────────────────────────────────────────────────
# Email alert
# ─────────────────────────────────────────────────────────────────────

def send_draft_alert_email(drafts: list[dict],
                           to_email: str = "mafe.cubillos@monvita.co",
                           dashboard_url: str = "https://nyvos-inventory.streamlit.app/"
                           ) -> tuple[bool, str]:
    """Sends (not drafts) a summary email to María with the new drafts.
    No-op when `drafts` is empty."""
    if not drafts:
        return True, "No había drafts nuevos para notificar."

    import base64
    from email.mime.text import MIMEText
    from googleapiclient.discovery import build
    from backend.google_oauth import get_credentials

    lines, grand_total = [], 0.0
    for d in drafts:
        po   = d["po"]
        p    = d["product"]
        qty  = d["final_qty"]
        unit = float(p["unit_price"] or 0)
        line_total = qty * unit
        grand_total += line_total
        lines.append(
            f"• OC {po['po_number']}: {p['name']}  —  "
            f"{qty:,} ud × {unit:,.2f}€ = {line_total:,.2f}€"
        )
    iva  = round(grand_total * 0.10, 2)
    tot  = grand_total + iva

    body = (
        f"Hola María,\n\n"
        f"{len(drafts)} nueva{'s' if len(drafts) != 1 else ''} orden"
        f"{'es' if len(drafts) != 1 else ''} de compra pendiente"
        f"{'s' if len(drafts) != 1 else ''} de aprobación:\n\n"
        + "\n".join(lines)
        + f"\n\n──────────────\n"
          f"Subtotal:  {grand_total:,.2f} €\n"
          f"IVA 10%:   {iva:,.2f} €\n"
          f"TOTAL:     {tot:,.2f} €\n\n"
          f"Revisa y aprueba en el dashboard:\n{dashboard_url}\n\n"
          f"— Operations Hub"
    )

    msg = MIMEText(body, "plain", "utf-8")
    msg["to"]      = to_email
    msg["subject"] = (f"{len(drafts)} nueva{'s' if len(drafts)!=1 else ''} "
                      f"PO pendiente{'s' if len(drafts)!=1 else ''} de aprobación")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    try:
        svc = build("gmail", "v1", credentials=get_credentials(),
                    cache_discovery=False)
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as exc:
        return False, f"Error enviando email: {exc}"
    return True, f"Enviado a {to_email}."
