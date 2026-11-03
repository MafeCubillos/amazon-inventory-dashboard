"""Purchase orders — create, list, and render-to-PDF Monvita's POs so
María can replace the manual Google-Sheets → download-as-PDF flow with
one dashboard button.

The PDF intentionally mirrors her sheet's layout: blue header bar,
left-aligned company block, 'Orden de compra # N' title, Fecha/Factura/
Número de orden row, supplier block, items table, totals block.
"""
from __future__ import annotations

import io
from datetime import date, datetime
from typing import Any

from backend.database import db_admin


# ─────────────────────────────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────────────────────────────

def get_company_profile() -> dict:
    rows = (db_admin.table("po_company_profile")
            .select("*").eq("id", 1).limit(1).execute().data) or []
    return rows[0] if rows else {}


def list_suppliers() -> list[dict]:
    return (db_admin.table("po_suppliers")
            .select("*").order("name").execute().data) or []


def get_supplier(sid: int) -> dict | None:
    rows = (db_admin.table("po_suppliers")
            .select("*").eq("id", sid).limit(1).execute().data) or []
    return rows[0] if rows else None


def next_po_number() -> int:
    """Last used PO number + 1 (first real one will be 87)."""
    rows = (db_admin.table("po_orders")
            .select("po_number")
            .order("po_number", desc=True)
            .limit(1).execute().data) or []
    return (rows[0]["po_number"] + 1) if rows else 1


def list_purchase_orders(limit: int = 200) -> list[dict]:
    rows = (db_admin.table("po_orders")
            .select("*, po_suppliers(name)")
            .order("po_number", desc=True)
            .limit(limit).execute().data) or []
    for r in rows:
        sup = r.pop("po_suppliers", None) or {}
        r["supplier_name"] = sup.get("name", "—")
    return rows


def get_purchase_order(po_id: int) -> dict | None:
    rows = (db_admin.table("po_orders")
            .select("*").eq("id", po_id).limit(1).execute().data) or []
    if not rows:
        return None
    po = rows[0]
    po["items"] = (db_admin.table("po_order_items")
                   .select("*").eq("po_id", po_id)
                   .order("position").execute().data) or []
    po["supplier"] = get_supplier(po["supplier_id"])
    return po


# ─────────────────────────────────────────────────────────────────────
# Writes
# ─────────────────────────────────────────────────────────────────────

def create_purchase_order(*,
                          supplier_id: int,
                          items: list[dict],
                          po_date: date | str | None = None,
                          invoice_ref: str | None = None,
                          iva_rate: float = 10.0,
                          currency: str = "EUR",
                          notes: str | None = None) -> dict:
    """items: list of {description, quantity, unit_price}. Returns the
    inserted PO row (with its new po_number)."""
    po_number = next_po_number()
    po_date_iso = (po_date.isoformat() if isinstance(po_date, date)
                   else (po_date or date.today().isoformat()))
    po_row = {
        "po_number":   po_number,
        "po_date":     po_date_iso,
        "supplier_id": supplier_id,
        "invoice_ref": invoice_ref,
        "iva_rate":    iva_rate,
        "currency":    currency,
        "status":      "draft",
        "notes":       notes,
    }
    po = (db_admin.table("po_orders")
          .insert(po_row).execute().data or [{}])[0]
    po_id = po["id"]

    item_rows = []
    for i, it in enumerate(items, start=1):
        desc = (it.get("description") or "").strip()
        if not desc:
            continue
        item_rows.append({
            "po_id":       po_id,
            "position":    i,
            "description": desc,
            "quantity":    float(it.get("quantity") or 0),
            "unit_price":  float(it.get("unit_price") or 0),
        })
    if item_rows:
        db_admin.table("po_order_items").insert(item_rows).execute()
    return get_purchase_order(po_id)


def update_po_status(po_id: int, status: str) -> None:
    db_admin.table("po_orders").update({
        "status": status,
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", po_id).execute()


def delete_po(po_id: int) -> None:
    db_admin.table("po_orders").delete().eq("id", po_id).execute()


# ─────────────────────────────────────────────────────────────────────
# Suppliers CRUD
# ─────────────────────────────────────────────────────────────────────

def upsert_supplier(data: dict) -> dict:
    if data.get("id"):
        db_admin.table("po_suppliers").update(
            {k: v for k, v in data.items() if k != "id"}
        ).eq("id", data["id"]).execute()
        return get_supplier(data["id"])
    row = (db_admin.table("po_suppliers").insert(data).execute().data or [{}])[0]
    return row


def delete_supplier(sid: int) -> None:
    db_admin.table("po_suppliers").delete().eq("id", sid).execute()


# ─────────────────────────────────────────────────────────────────────
# Company profile
# ─────────────────────────────────────────────────────────────────────

def update_company_profile(data: dict) -> dict:
    db_admin.table("po_company_profile").update(
        {k: v for k, v in data.items() if k != "id"}
    ).eq("id", 1).execute()
    return get_company_profile()


# ─────────────────────────────────────────────────────────────────────
# PDF rendering (reportlab) — mirrors Monvita's sheet layout
# ─────────────────────────────────────────────────────────────────────

_BLUE  = "#1F4E79"       # titles, section headers, totals
_RULE  = "#DDDDDD"       # hairline dividers
_MUTED = "#555555"       # labels


def _fmt_money(v: float, currency: str = "EUR") -> str:
    sym = {"EUR": "€", "USD": "$"}.get(currency, "")
    # 1,234.56 — same style as the current sheet
    s = f"{v:,.2f}"
    return f"{sym}{s}" if currency == "USD" else f"{s} {sym}".strip()


def _fmt_qty(v: float) -> str:
    # Integer when whole, else up to 3 decimals (matches "1,041")
    if float(v).is_integer():
        return f"{int(v):,}"
    return f"{v:,.3f}".rstrip("0").rstrip(".")


def render_po_pdf(po: dict) -> bytes:
    """Render a PO dict (as returned by get_purchase_order) into PDF bytes."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                     Table, TableStyle, HRFlowable)
    from reportlab.lib import colors

    company = get_company_profile()
    supplier = po.get("supplier") or {}
    items    = po.get("items") or []
    currency = po.get("currency") or "EUR"
    iva_rate = float(po.get("iva_rate") or 10.0)

    subtotal = sum(float(it.get("line_total")
                         or (float(it["quantity"]) * float(it["unit_price"])))
                   for it in items)
    iva_amt  = round(subtotal * iva_rate / 100.0, 2)
    total    = round(subtotal + iva_amt, 2)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=22*mm, rightMargin=22*mm,
                            topMargin=16*mm, bottomMargin=18*mm,
                            title=f"Orden de compra {po['po_number']}")

    styles = getSampleStyleSheet()
    S = {
        "company":   ParagraphStyle("company", parent=styles["Normal"],
                                    fontName="Helvetica", fontSize=18,
                                    textColor=colors.HexColor(_BLUE),
                                    leading=22, spaceAfter=2),
        "addr":      ParagraphStyle("addr", parent=styles["Normal"],
                                    fontName="Helvetica", fontSize=9,
                                    textColor=colors.HexColor("#333333"),
                                    leading=12),
        "title":     ParagraphStyle("title", parent=styles["Normal"],
                                    fontName="Helvetica-Bold", fontSize=22,
                                    textColor=colors.HexColor(_BLUE),
                                    spaceBefore=14, spaceAfter=10, leading=26),
        "lbl":       ParagraphStyle("lbl", parent=styles["Normal"],
                                    fontName="Helvetica-Bold", fontSize=9,
                                    textColor=colors.HexColor("#222222"),
                                    leading=12),
        "val":       ParagraphStyle("val", parent=styles["Normal"],
                                    fontName="Helvetica", fontSize=9,
                                    textColor=colors.HexColor("#333333"),
                                    leading=12),
        "section":   ParagraphStyle("section", parent=styles["Normal"],
                                    fontName="Helvetica-Bold", fontSize=10,
                                    textColor=colors.HexColor("#111111"),
                                    spaceBefore=10, spaceAfter=4, leading=14),
        "sup_name":  ParagraphStyle("sup_name", parent=styles["Normal"],
                                    fontName="Helvetica-Bold", fontSize=10,
                                    leading=13, spaceAfter=1),
        "th":        ParagraphStyle("th", parent=styles["Normal"],
                                    fontName="Helvetica-Bold", fontSize=10,
                                    textColor=colors.HexColor(_BLUE), leading=12),
        "tdesc":     ParagraphStyle("tdesc", parent=styles["Normal"],
                                    fontName="Helvetica", fontSize=8.5,
                                    leading=11),
        "tnum":      ParagraphStyle("tnum", parent=styles["Normal"],
                                    fontName="Helvetica", fontSize=9,
                                    alignment=TA_RIGHT, leading=12),
        "tot_lbl":   ParagraphStyle("tot_lbl", parent=styles["Normal"],
                                    fontName="Helvetica", fontSize=10,
                                    textColor=colors.HexColor(_BLUE),
                                    alignment=TA_RIGHT, leading=14),
        "tot_val":   ParagraphStyle("tot_val", parent=styles["Normal"],
                                    fontName="Helvetica-Bold", fontSize=10,
                                    alignment=TA_RIGHT, leading=14),
        "tot_big_lbl": ParagraphStyle("tot_big_lbl", parent=styles["Normal"],
                                      fontName="Helvetica-Bold", fontSize=11,
                                      textColor=colors.HexColor(_BLUE),
                                      alignment=TA_RIGHT, leading=16),
        "tot_big_val": ParagraphStyle("tot_big_val", parent=styles["Normal"],
                                      fontName="Helvetica-Bold", fontSize=11,
                                      alignment=TA_RIGHT, leading=16),
    }

    def _addr(lines: str) -> list:
        out = []
        for ln in (lines or "").splitlines():
            out.append(Paragraph(ln.strip(), S["addr"]))
        return out

    story: list = []

    # Blue top rule
    story.append(HRFlowable(width="100%", thickness=4,
                            color=colors.HexColor(_BLUE),
                            spaceBefore=0, spaceAfter=10))

    # Company header
    story.append(Paragraph(company.get("name", ""), S["company"]))
    story.extend(_addr(company.get("address_lines", "")))
    story.append(Paragraph(f'CIF/NIF: {company.get("cif", "")}', S["addr"]))

    # Title
    story.append(Paragraph(f"Orden de compra # {po['po_number']}", S["title"]))

    # Fecha / Factura / Número — three-column strip, same as sheet
    po_date_str = ""
    if po.get("po_date"):
        try:
            dt = datetime.fromisoformat(str(po["po_date"]))
        except ValueError:
            dt = datetime.strptime(str(po["po_date"])[:10], "%Y-%m-%d")
        po_date_str = dt.strftime("%-d/%-m/%Y")

    meta_tbl = Table([
        [Paragraph("Fecha",             S["lbl"]),
         Paragraph("Factura",           S["lbl"]),
         Paragraph("Numero de orden",   S["lbl"])],
        [Paragraph(po_date_str,                     S["val"]),
         Paragraph(po.get("invoice_ref") or "",     S["val"]),
         Paragraph(str(po["po_number"]),            S["val"])],
    ], colWidths=[55*mm, 55*mm, 55*mm])
    meta_tbl.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING",  (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("BOTTOMPADDING",(0,0), (-1,0),  4),
    ]))
    story.append(meta_tbl)
    story.append(HRFlowable(width="100%", thickness=0.5,
                            color=colors.HexColor(_RULE),
                            spaceBefore=8, spaceAfter=8))

    # Proveedor
    story.append(Paragraph("Proveedor", S["section"]))
    story.append(Paragraph(supplier.get("name", ""), S["sup_name"]))
    story.extend(_addr(supplier.get("address_lines", "")))
    if supplier.get("phone"):
        story.append(Paragraph(f'Teléfono: {supplier["phone"]}', S["addr"]))
    if supplier.get("web"):
        story.append(Paragraph(f'Web: {supplier["web"]}', S["addr"]))
    if supplier.get("nif"):
        story.append(Paragraph(f'CIF/NIF: {supplier["nif"]}', S["addr"]))
    story.append(HRFlowable(width="100%", thickness=0.5,
                            color=colors.HexColor(_RULE),
                            spaceBefore=10, spaceAfter=6))

    # Items table
    data = [[Paragraph("Descripción", S["th"]),
             Paragraph("Cant",        S["th"]),
             Paragraph("Precio Und",  S["th"]),
             Paragraph("Precio total",S["th"])]]
    for it in items:
        qty = float(it["quantity"])
        unit = float(it["unit_price"])
        total_line = float(it.get("line_total") or qty * unit)
        data.append([
            Paragraph((it["description"] or "").replace("\n", "<br/>"), S["tdesc"]),
            Paragraph(_fmt_qty(qty), S["tnum"]),
            Paragraph(_fmt_money(unit, currency), S["tnum"]),
            Paragraph(_fmt_money(total_line, currency), S["tnum"]),
        ])
    items_tbl = Table(data, colWidths=[95*mm, 18*mm, 25*mm, 28*mm], repeatRows=1)
    items_tbl.setStyle(TableStyle([
        ("VALIGN",       (0,0), (-1,-1), "TOP"),
        ("BOTTOMPADDING",(0,0), (-1,0),  6),
        ("TOPPADDING",   (0,0), (-1,0),  4),
        ("BOTTOMPADDING",(0,1), (-1,-1), 8),
        ("TOPPADDING",   (0,1), (-1,-1), 8),
        ("LINEBELOW",    (0,0), (-1,0),  0.5, colors.HexColor(_RULE)),
        ("BACKGROUND",   (0,1), (-1,-1), colors.HexColor("#F8F8F8")),
        ("LEFTPADDING",  (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(items_tbl)

    # Totals block — right-aligned, no left columns
    totals_tbl = Table([
        [Paragraph("Subtotal",             S["tot_lbl"]),
         Paragraph(_fmt_money(subtotal, currency), S["tot_val"])],
        [Paragraph(f"IVA {iva_rate:g}%",   S["tot_lbl"]),
         Paragraph(_fmt_money(iva_amt,  currency), S["tot_val"])],
        [Paragraph("TOTAL",                S["tot_big_lbl"]),
         Paragraph(_fmt_money(total,    currency), S["tot_big_val"])],
    ], colWidths=[138*mm, 28*mm])
    totals_tbl.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING",  (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ("TOPPADDING",   (0,0), (-1,-1), 2),
        ("BOTTOMPADDING",(0,0), (-1,-1), 2),
        ("LINEABOVE",    (0,-1),(-1,-1), 0.5, colors.HexColor(_RULE)),
    ]))
    story.append(Spacer(1, 2))
    story.append(totals_tbl)

    doc.build(story)
    return buf.getvalue()
