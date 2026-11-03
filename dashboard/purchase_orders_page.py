"""Purchase Orders page — list, create, download PDF.
Replaces María's manual Google-Sheets → PDF → Drive flow.

Fase 2 (not yet wired): "Guardar en Drive" + "Crear borrador en Gmail"
buttons will appear on each PO's detail view once OAuth is configured.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
from datetime import date

from backend.purchase_orders import (
    create_purchase_order, delete_po, delete_product, delete_supplier,
    get_company_profile, get_purchase_order, list_products,
    list_purchase_orders, list_suppliers, next_po_number, render_po_pdf,
    update_company_profile, update_po_status, upsert_product,
    upsert_supplier,
)


# ─────────────────────────────────────────────────────────────────────
def render_purchase_orders_page():
    st.title("🧾 Órdenes de compra")

    tab_list, tab_new, tab_products, tab_suppliers, tab_company = st.tabs([
        "📋 Historial",
        "➕ Nueva orden",
        "📦 Productos",
        "🏢 Proveedores",
        "⚙️ Datos de empresa",
    ])

    with tab_list:
        _render_list()
    with tab_new:
        _render_new()
    with tab_products:
        _render_products()
    with tab_suppliers:
        _render_suppliers()
    with tab_company:
        _render_company()


# ─────────────────────────────────────────────────────────────────────
# Historial
# ─────────────────────────────────────────────────────────────────────
def _render_list():
    pos = list_purchase_orders()
    pos = [p for p in pos if p.get("status") != "cancelled"
           or p.get("notes", "").lower().find("placeholder") < 0]
    if not pos:
        st.info("Aún no hay órdenes de compra. Ve a **➕ Nueva orden**.")
        return

    df = pd.DataFrame([{
        "PO #":        p["po_number"],
        "Fecha":       p["po_date"],
        "Proveedor":   p.get("supplier_name", "—"),
        "Factura":     p.get("invoice_ref") or "",
        "Estado":      p["status"],
        "id":          p["id"],
    } for p in pos])
    st.dataframe(df.drop(columns=["id"]), use_container_width=True,
                 hide_index=True)

    st.markdown("##### 🔎 Ver / descargar PDF")
    choices = {f"PO #{p['po_number']} — {p.get('supplier_name','—')} ({p['po_date']})": p["id"]
               for p in pos}
    pick = st.selectbox("Selecciona una orden", list(choices.keys()),
                        key="po_pick_view")
    if pick:
        po = get_purchase_order(choices[pick])
        _render_po_detail(po)


def _render_po_detail(po: dict):
    sup = po.get("supplier") or {}
    items = po.get("items") or []
    subtotal = sum(float(it.get("line_total") or
                         float(it["quantity"]) * float(it["unit_price"]))
                   for it in items)
    iva = round(subtotal * float(po["iva_rate"]) / 100.0, 2)

    c1, c2, c3 = st.columns([2, 2, 1])
    c1.metric("Proveedor", sup.get("name", "—"))
    c2.metric("Fecha", str(po["po_date"]))
    c3.metric("Total", f"{subtotal + iva:,.2f} {po.get('currency','EUR')}")

    st.caption(f"IVA {po['iva_rate']}% · Estado: **{po['status']}**")

    try:
        pdf_bytes = render_po_pdf(po)
        st.download_button(
            f"⬇️ Descargar PDF — Orden de compra {po['po_number']}.pdf",
            data=pdf_bytes,
            file_name=f"Orden de compra {po['po_number']}.pdf",
            mime="application/pdf",
            key=f"dl_{po['id']}",
        )
    except ModuleNotFoundError:
        st.error("Falta `reportlab`. Añade `reportlab>=4.0` a requirements.txt.")

    with st.expander("Items"):
        st.dataframe(pd.DataFrame([{
            "Descripción": it["description"],
            "Cant":        it["quantity"],
            "Precio Und":  it["unit_price"],
            "Precio total": it.get("line_total"),
        } for it in items]), use_container_width=True, hide_index=True)

    c1, c2, c3 = st.columns(3)
    new_status = c1.selectbox("Cambiar estado",
                              ["draft", "sent", "received", "cancelled"],
                              index=["draft", "sent", "received", "cancelled"]
                                    .index(po["status"]),
                              key=f"status_{po['id']}")
    if c2.button("💾 Guardar estado", key=f"save_status_{po['id']}"):
        update_po_status(po["id"], new_status)
        st.success("Actualizado.")
        st.rerun()
    if c3.button("🗑️ Eliminar", key=f"del_{po['id']}"):
        delete_po(po["id"])
        st.success(f"PO #{po['po_number']} eliminada.")
        st.rerun()


# ─────────────────────────────────────────────────────────────────────
# Nueva orden
# ─────────────────────────────────────────────────────────────────────
def _render_new():
    suppliers = list_suppliers()
    if not suppliers:
        st.warning("No hay proveedores. Añade uno en **🏢 Proveedores**.")
        return

    next_num = next_po_number()
    st.caption(f"Próximo número: **#{next_num}** (asignado al guardar)")

    sup_names = {f"{s['name']}": s["id"] for s in suppliers}
    sup_pick = st.selectbox("Proveedor", list(sup_names.keys()),
                            key="new_po_supplier")
    sup_id = sup_names[sup_pick]

    c1, c2, c3 = st.columns(3)
    po_date = c1.date_input("Fecha", value=date.today(), key="new_po_date")
    invoice_ref = c2.text_input("Factura (opcional)", key="new_po_invoice")
    iva_rate = c3.number_input("IVA %", value=10.0, min_value=0.0,
                               max_value=25.0, step=0.5, key="new_po_iva")

    # ── Items session-state buffer (so Add-from-catalog can push rows) ─
    key_items = "new_po_items_df"
    if key_items not in st.session_state:
        st.session_state[key_items] = pd.DataFrame(
            [{"Descripción": "", "Cantidad": 0, "Precio Unit": 0.0}])

    # ── Catalog picker: pick one product → appends a prefilled row ────
    products = list_products(supplier_id=sup_id)
    st.markdown("##### 📦 Items")
    if products:
        colA, colB = st.columns([4, 1])
        prod_labels = {f"{p['name']}  — MOQ {int(p['moq'])} · {p['unit_price']:.2f} €": p
                       for p in products}
        pick = colA.selectbox("Añadir desde catálogo",
                              ["— elige un producto —"] + list(prod_labels.keys()),
                              key="new_po_prod_pick")
        if colB.button("➕ Añadir", key="new_po_prod_add", use_container_width=True):
            if pick != "— elige un producto —":
                p = prod_labels[pick]
                new_row = pd.DataFrame([{
                    "Descripción": p["description"],
                    "Cantidad":    float(p["moq"]),
                    "Precio Unit": float(p["unit_price"]),
                }])
                # Drop a leading empty row if present
                buf = st.session_state[key_items]
                mask_empty = (buf["Descripción"].astype(str).str.strip() == "")
                buf = buf[~mask_empty] if mask_empty.all() else buf
                st.session_state[key_items] = pd.concat(
                    [buf, new_row], ignore_index=True)
                st.rerun()
    else:
        st.caption(f"ℹ️ No hay productos cargados para **{sup_pick}**. "
                   f"Añádelos en el tab **📦 Productos**.")

    st.caption("Edita las filas. Puedes modificar cantidad y precio si cambian.")

    edited = st.data_editor(
        st.session_state[key_items],
        num_rows="dynamic",
        use_container_width=True,
        key="new_po_items_editor",
        column_config={
            "Descripción": st.column_config.TextColumn(
                "Descripción", width="large",
                help="Multilinea permitida (Shift+Enter)."),
            "Cantidad":    st.column_config.NumberColumn(
                "Cant", min_value=0.0, step=1.0, format="%g"),
            "Precio Unit": st.column_config.NumberColumn(
                "Precio Und", min_value=0.0, step=0.01, format="%.4f"),
        },
    )
    st.session_state[key_items] = edited

    # Live preview totals
    valid = edited[edited["Descripción"].astype(str).str.strip() != ""]
    subtotal = float((valid["Cantidad"].fillna(0) *
                      valid["Precio Unit"].fillna(0)).sum())
    iva_amt  = round(subtotal * iva_rate / 100, 2)
    total    = round(subtotal + iva_amt, 2)
    c1, c2, c3 = st.columns(3)
    c1.metric("Subtotal", f"{subtotal:,.2f} €")
    c2.metric(f"IVA {iva_rate:g}%", f"{iva_amt:,.2f} €")
    c3.metric("TOTAL", f"{total:,.2f} €")

    notes = st.text_area("Notas internas (no van al PDF)", key="new_po_notes")

    if st.button("✅ Crear orden de compra", type="primary",
                 use_container_width=True):
        items = [{
            "description": r["Descripción"],
            "quantity":    r["Cantidad"],
            "unit_price":  r["Precio Unit"],
        } for _, r in valid.iterrows()]
        if not items:
            st.error("Añade al menos una línea.")
            return
        po = create_purchase_order(
            supplier_id=sup_id,
            items=items,
            po_date=po_date,
            invoice_ref=invoice_ref or None,
            iva_rate=iva_rate,
            notes=notes or None,
        )
        st.success(f"✅ PO #{po['po_number']} creada.")
        st.balloons()
        # Reset the items buffer for the next PO
        st.session_state[key_items] = pd.DataFrame(
            [{"Descripción": "", "Cantidad": 0, "Precio Unit": 0.0}])

        # Offer the PDF right away
        try:
            pdf = render_po_pdf(po)
            st.download_button(
                f"⬇️ Descargar PDF — Orden de compra {po['po_number']}.pdf",
                data=pdf,
                file_name=f"Orden de compra {po['po_number']}.pdf",
                mime="application/pdf",
                key=f"dl_new_{po['id']}",
            )
        except ModuleNotFoundError:
            st.warning("PDF no generado: falta `reportlab` en requirements.")


# ─────────────────────────────────────────────────────────────────────
# Productos
# ─────────────────────────────────────────────────────────────────────
def _render_products():
    products = list_products(only_active=False)
    suppliers = list_suppliers()
    sup_by_id = {s["id"]: s["name"] for s in suppliers}

    st.caption(f"{len(products)} producto(s) en catálogo. "
               "En **Nueva orden**, elige el proveedor y luego el producto "
               "para precargar una fila con descripción, MOQ y precio.")

    if products:
        df = pd.DataFrame([{
            "ID":          p["id"],
            "Nombre":      p["name"],
            "Proveedor":   p.get("supplier_name") or "—",
            "MOQ":         p["moq"],
            "Precio":      p["unit_price"],
            "Activo":      "✓" if p.get("active") else "—",
            "Descripción (resumen)": (p["description"][:80] + "…"
                                       if len(p["description"]) > 80
                                       else p["description"]).replace("\n", " · "),
        } for p in products])
        st.dataframe(df, use_container_width=True, hide_index=True)

    with st.expander("➕ Añadir o editar producto"):
        prod_labels = ["— nuevo —"] + [
            f"{p['name']}  —  {p.get('supplier_name','—')}" for p in products]
        pick = st.selectbox("Editar existente (o '— nuevo —' para crear)",
                             prod_labels, key="prod_edit_pick")
        current = None
        if pick != "— nuevo —":
            idx = prod_labels.index(pick) - 1
            current = products[idx]

        name = st.text_input("Nombre corto", value=(current or {}).get("name", ""),
                              key="prod_name")
        desc = st.text_area("Descripción completa (va al PDF)",
                             value=(current or {}).get("description", ""),
                             height=160, key="prod_desc",
                             help="Usa saltos de línea para la composición.")
        c1, c2, c3 = st.columns(3)
        price = c1.number_input("Precio unitario (€)",
                                 value=float((current or {}).get("unit_price") or 0),
                                 min_value=0.0, step=0.01, format="%.4f",
                                 key="prod_price")
        moq   = c2.number_input("MOQ (cantidad mínima)",
                                 value=float((current or {}).get("moq") or 0),
                                 min_value=0.0, step=1.0, format="%g",
                                 key="prod_moq")
        sup_options = {s["name"]: s["id"] for s in suppliers}
        sup_names_list = ["(ninguno)"] + list(sup_options.keys())
        default_sup = (sup_by_id.get((current or {}).get("default_supplier_id"))
                       or "(ninguno)")
        sup_pick = c3.selectbox("Proveedor por defecto",
                                 sup_names_list,
                                 index=sup_names_list.index(default_sup)
                                       if default_sup in sup_names_list else 0,
                                 key="prod_supplier")
        active = st.checkbox("Activo (aparece en el selector de catálogo)",
                              value=bool((current or {}).get("active", True)),
                              key="prod_active")

        c1, c2 = st.columns(2)
        if c1.button("💾 Guardar producto", type="primary", key="prod_save"):
            if not name.strip() or not desc.strip():
                st.error("Nombre y descripción son obligatorios.")
            else:
                data = {
                    "name":                name.strip(),
                    "description":         desc.strip(),
                    "unit_price":          price,
                    "moq":                 moq,
                    "default_supplier_id": sup_options.get(sup_pick)
                                           if sup_pick != "(ninguno)" else None,
                    "active":              active,
                }
                if current:
                    data["id"] = current["id"]
                upsert_product(data)
                st.success("Guardado.")
                st.rerun()
        if current and c2.button("🗑️ Eliminar este producto", key="prod_delete"):
            delete_product(current["id"])
            st.success("Eliminado.")
            st.rerun()


# ─────────────────────────────────────────────────────────────────────
# Proveedores
# ─────────────────────────────────────────────────────────────────────
def _render_suppliers():
    suppliers = list_suppliers()
    st.caption(f"{len(suppliers)} proveedor(es) registrados.")

    if suppliers:
        df = pd.DataFrame([{
            "ID":        s["id"],
            "Nombre":    s["name"],
            "NIF":       s.get("nif") or "",
            "Teléfono":  s.get("phone") or "",
            "Email":     s.get("email") or "",
            "Web":       s.get("web") or "",
            "Dirección": (s.get("address_lines") or "").replace("\n", " · "),
        } for s in suppliers])
        st.dataframe(df, use_container_width=True, hide_index=True)

    with st.expander("➕ Añadir o editar proveedor"):
        sup_pick = st.selectbox(
            "Editar existente (o déjalo en blanco para crear nuevo)",
            ["— nuevo —"] + [s["name"] for s in suppliers],
            key="sup_edit_pick")
        current = next((s for s in suppliers if s["name"] == sup_pick), None)

        name    = st.text_input("Nombre", value=(current or {}).get("name", ""),
                                 key="sup_name")
        address = st.text_area("Dirección (una línea por fila)",
                                value=(current or {}).get("address_lines", "") or "",
                                key="sup_address")
        c1, c2 = st.columns(2)
        nif   = c1.text_input("NIF/CIF",  value=(current or {}).get("nif", "") or "",
                              key="sup_nif")
        phone = c2.text_input("Teléfono", value=(current or {}).get("phone", "") or "",
                              key="sup_phone")
        c3, c4 = st.columns(2)
        email = c3.text_input("Email", value=(current or {}).get("email", "") or "",
                              key="sup_email")
        web   = c4.text_input("Web",   value=(current or {}).get("web", "")   or "",
                              key="sup_web")

        c1, c2 = st.columns(2)
        if c1.button("💾 Guardar proveedor", type="primary", key="sup_save"):
            if not name.strip():
                st.error("El nombre es obligatorio.")
            else:
                data = {"name": name.strip(),
                        "address_lines": address.strip() or None,
                        "nif": nif.strip() or None,
                        "phone": phone.strip() or None,
                        "email": email.strip() or None,
                        "web": web.strip() or None}
                if current:
                    data["id"] = current["id"]
                upsert_supplier(data)
                st.success("Guardado.")
                st.rerun()
        if current and c2.button("🗑️ Eliminar este proveedor", key="sup_delete"):
            delete_supplier(current["id"])
            st.success("Eliminado.")
            st.rerun()


# ─────────────────────────────────────────────────────────────────────
# Datos empresa
# ─────────────────────────────────────────────────────────────────────
def _render_company():
    comp = get_company_profile()
    st.caption("Estos datos aparecen en la cabecera del PDF.")

    name    = st.text_input("Nombre", value=comp.get("name", ""), key="co_name")
    address = st.text_area("Dirección (una línea por fila)",
                            value=comp.get("address_lines", ""), key="co_address")
    c1, c2 = st.columns(2)
    cif    = c1.text_input("CIF/NIF",  value=comp.get("cif", ""),       key="co_cif")
    phone  = c2.text_input("Teléfono", value=comp.get("phone", "") or "", key="co_phone")
    c3, c4 = st.columns(2)
    email  = c3.text_input("Email", value=comp.get("email", "") or "", key="co_email")
    curr   = c4.selectbox("Moneda", ["EUR", "USD"],
                           index=0 if comp.get("currency", "EUR") == "EUR" else 1,
                           key="co_currency")

    if st.button("💾 Guardar", type="primary", key="co_save"):
        update_company_profile({"name": name, "address_lines": address,
                                 "cif": cif, "phone": phone or None,
                                 "email": email or None, "currency": curr})
        st.success("Guardado.")
        st.rerun()
