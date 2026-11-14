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
from backend.google_oauth import (
    build_auth_url, disconnect, exchange_code, get_google_tokens,
    is_connected, save_client,
)
from backend.google_services import create_gmail_draft, upload_po_to_drive
from backend.po_auto_draft import scan_and_create_drafts, send_draft_alert_email
from dashboard.nyvos_ui import render_nyvos_df


# ─────────────────────────────────────────────────────────────────────
def render_purchase_orders_page():
    st.title("🧾 Órdenes de compra")

    # If Google just redirected back with ?code=…, finish the OAuth
    # exchange before rendering anything else.
    _handle_oauth_callback()

    (tab_list, tab_new, tab_products, tab_suppliers,
     tab_google, tab_company) = st.tabs([
        "📋 Historial",
        "➕ Nueva orden",
        "📦 Productos",
        "🏢 Proveedores",
        "🔗 Google (Drive + Gmail)",
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
    with tab_google:
        _render_google()
    with tab_company:
        _render_company()


# ─────────────────────────────────────────────────────────────────────
# Historial
# ─────────────────────────────────────────────────────────────────────
def _render_list():
    _render_auto_scan_banner()

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
    render_nyvos_df(
        df.drop(columns=["id"]),
        widths={"PO #": "90px", "Fecha": "100px", "Estado": "90px", "Factura": "120px"},
        aligns={"Estado": "center"},
    )

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
        render_nyvos_df(
            pd.DataFrame([{
                "Descripción":  it["description"],
                "Cant":         it["quantity"],
                "Precio Und":   it["unit_price"],
                "Precio total": it.get("line_total"),
            } for it in items]),
            widths={"Descripción": "auto", "Cant": "90px",
                    "Precio Und": "120px", "Precio total": "130px"},
        )

    # ── Google integration buttons (Drive + Gmail) ───────────────
    if is_connected():
        gc1, gc2 = st.columns(2)
        drive_label = ("📤 Subir a Drive" if not po.get("drive_file_id")
                       else "📤 Volver a subir a Drive")
        if gc1.button(drive_label, key=f"drive_{po['id']}",
                      use_container_width=True):
            with st.spinner("Subiendo a Google Drive..."):
                ok, msg, _ = upload_po_to_drive(po["id"])
            if ok:
                st.success(f"✅ Subido. [Abrir en Drive]({msg})")
                st.rerun()
            else:
                st.error(msg)
        if po.get("drive_file_id"):
            gc1.caption(
                f"✓ Última subida: "
                f"[archivo en Drive](https://drive.google.com/file/d/{po['drive_file_id']})")

        gmail_label = ("✉️ Crear borrador email al proveedor"
                       if not po.get("gmail_draft_id")
                       else "✉️ Volver a crear borrador")
        if gc2.button(gmail_label, key=f"gmail_{po['id']}",
                      use_container_width=True):
            with st.spinner("Creando borrador en Gmail..."):
                ok, msg, draft_id = create_gmail_draft(po["id"])
            if ok:
                st.success("✅ Borrador creado. Ábrelo en Gmail → "
                           "[Borradores](https://mail.google.com/mail/u/0/#drafts) "
                           "para revisarlo y enviar.")
                st.rerun()
            else:
                st.error(msg)
        if po.get("gmail_draft_id"):
            gc2.caption("✓ Borrador ya existente — se sobreescribe si vuelves a pulsar.")
    else:
        st.info("💡 Conecta Google en el tab **🔗 Google** para subir "
                "automáticamente a Drive y crear borradores de email.")

    c1, c2, c3 = st.columns(3)
    new_status = c1.selectbox("Cambiar estado",
                              ["draft", "sent", "received", "cancelled"],
                              index=["draft", "sent", "received", "cancelled"]
                                    .index(po["status"]),
                              key=f"status_{po['id']}")
    if c2.button("💾 Guardar estado", key=f"save_status_{po['id']}"):
        ok, msg = update_po_status(po["id"], new_status)
        if ok:
            st.success(f"Actualizado. {msg}")
            st.rerun()
        else:
            # Don't rerun on error so María can read the real exception
            st.error(f"Estado guardado en Purchase Orders, pero fallo al sincronizar con Orders:\n\n{msg}")
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

    c1, c2 = st.columns(2)
    po_date = c1.date_input("Fecha", value=date.today(), key="new_po_date")
    iva_rate = c2.number_input("IVA %", value=10.0, min_value=0.0,
                               max_value=25.0, step=0.5, key="new_po_iva")
    invoice_ref = None

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
        render_nyvos_df(
            df,
            widths={"Nombre": "200px", "Proveedor": "150px", "MOQ": "80px",
                    "Precio": "90px", "Activo": "70px", "Descripción (resumen)": "auto"},
            aligns={"Activo": "center"},
        )

    with st.expander("➕ Añadir o editar producto"):
        _PROD_KEYS = ("prod_name", "prod_desc", "prod_drive", "prod_price",
                      "prod_moq", "prod_supplier", "prod_active", "prod_asins")

        def _on_prod_change():
            for k in _PROD_KEYS:
                st.session_state.pop(k, None)

        prod_labels = ["— nuevo —"] + [
            f"{p['name']}  —  {p.get('supplier_name','—')}" for p in products]
        pick = st.selectbox("Editar existente (o '— nuevo —' para crear)",
                             prod_labels, key="prod_edit_pick",
                             on_change=_on_prod_change)
        current = None
        if pick != "— nuevo —":
            idx = prod_labels.index(pick) - 1
            current = products[idx]

        sup_options = {s["name"]: s["id"] for s in suppliers}
        sup_names_list = ["(ninguno)"] + list(sup_options.keys())
        default_sup = (sup_by_id.get((current or {}).get("default_supplier_id"))
                       or "(ninguno)")

        # Seed state on first show / after switch — every widget key, so
        # switching between products refreshes the inputs properly.
        if "prod_name" not in st.session_state:
            st.session_state["prod_name"] = (current or {}).get("name", "")
        if "prod_desc" not in st.session_state:
            st.session_state["prod_desc"] = (current or {}).get("description", "")
        if "prod_drive" not in st.session_state:
            st.session_state["prod_drive"] = (current or {}).get("drive_folder_id", "") or ""
        if "prod_price" not in st.session_state:
            st.session_state["prod_price"] = float((current or {}).get("unit_price") or 0)
        if "prod_moq" not in st.session_state:
            st.session_state["prod_moq"] = float((current or {}).get("moq") or 0)
        if "prod_supplier" not in st.session_state:
            st.session_state["prod_supplier"] = default_sup
        if "prod_active" not in st.session_state:
            st.session_state["prod_active"] = bool((current or {}).get("active", True))
        if "prod_asins" not in st.session_state:
            st.session_state["prod_asins"] = ", ".join((current or {}).get("asins") or [])

        name = st.text_input("Nombre corto", key="prod_name")
        desc = st.text_area("Descripción completa (va al PDF)",
                             height=160, key="prod_desc",
                             help="Usa saltos de línea para la composición.")
        c1, c2, c3 = st.columns(3)
        price = c1.number_input("Precio unitario (€)",
                                 min_value=0.0, step=0.01, format="%.4f",
                                 key="prod_price")
        moq   = c2.number_input("MOQ (cantidad mínima)",
                                 min_value=0.0, step=1.0, format="%g",
                                 key="prod_moq")
        sup_pick = c3.selectbox("Proveedor por defecto",
                                 sup_names_list, key="prod_supplier")
        active = st.checkbox("Activo (aparece en el selector de catálogo)",
                              key="prod_active")
        drive_folder = st.text_input(
            "Drive folder ID (opcional)",
            key="prod_drive",
            help=("ID de la carpeta de Drive donde guardar las POs de este producto. "
                  "Lo sacas de la URL: drive.google.com/drive/folders/[este-id]"))

        asins_text = st.text_input(
            "ASINs de Amazon (separados por coma)",
            key="prod_asins",
            help=("ASINs del producto en cada marketplace (ES, FR, IT, DE…). "
                  "Se usan para que el auto-draft de Fase 3 sepa qué producto "
                  "del catálogo corresponde a cada sugerencia de Reorder. "
                  "Ej: B0C1234567, B0C7654321"))

        c1, c2 = st.columns(2)
        if c1.button("💾 Guardar producto", type="primary", key="prod_save"):
            if not name.strip() or not desc.strip():
                st.error("Nombre y descripción son obligatorios.")
            else:
                asins_list = [a.strip().upper() for a in asins_text.split(",")
                              if a.strip()]
                data = {
                    "name":                name.strip(),
                    "description":         desc.strip(),
                    "unit_price":          price,
                    "moq":                 moq,
                    "default_supplier_id": sup_options.get(sup_pick)
                                           if sup_pick != "(ninguno)" else None,
                    "active":              active,
                    "drive_folder_id":     drive_folder.strip() or None,
                    "asins":               asins_list,
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
        render_nyvos_df(
            df,
            widths={"ID": "60px", "Nombre": "180px", "NIF": "120px",
                    "Teléfono": "120px", "Email": "180px",
                    "Web": "140px", "Dirección": "auto"},
        )

    with st.expander("➕ Añadir o editar proveedor"):
        # Reset field state when the dropdown switches so edits of one
        # supplier never bleed into another one.
        def _on_sup_change():
            for k in ("sup_name", "sup_address", "sup_nif",
                      "sup_phone", "sup_email", "sup_web", "sup_contact"):
                st.session_state.pop(k, None)

        sup_pick = st.selectbox(
            "Editar existente (o déjalo en blanco para crear nuevo)",
            ["— nuevo —"] + [s["name"] for s in suppliers],
            key="sup_edit_pick", on_change=_on_sup_change)
        current = next((s for s in suppliers if s["name"] == sup_pick), None)

        # Pre-seed session_state the first time a supplier is selected,
        # so text_inputs reflect its data; subsequent edits are preserved.
        for state_key, src_key in [("sup_name", "name"),
                                    ("sup_address", "address_lines"),
                                    ("sup_nif", "nif"), ("sup_phone", "phone"),
                                    ("sup_email", "email"), ("sup_web", "web"),
                                    ("sup_contact", "contact_name")]:
            if state_key not in st.session_state:
                st.session_state[state_key] = (current or {}).get(src_key, "") or ""

        name    = st.text_input("Nombre",                               key="sup_name")
        address = st.text_area("Dirección (una línea por fila)",        key="sup_address")
        c1, c2 = st.columns(2)
        nif   = c1.text_input("NIF/CIF",  key="sup_nif")
        phone = c2.text_input("Teléfono", key="sup_phone")
        c3, c4 = st.columns(2)
        email = c3.text_input("Email",    key="sup_email")
        web   = c4.text_input("Web",      key="sup_web")
        contact = st.text_input(
            "Nombre de contacto (para el saludo del email)",
            key="sup_contact",
            help="Ej: 'Javier'. Se usará como 'Hola Javier,' en el borrador de email.")

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
                        "web": web.strip() or None,
                        "contact_name": contact.strip() or None}
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

    cc = st.text_input(
        "Emails en copia (CC) para todos los borradores a proveedores",
        value=comp.get("cc_emails", "") or "",
        key="co_cc",
        help="Separa varios con coma. Ej: carlos.zuniga@monvita.co, otro@monvita.co")

    if st.button("💾 Guardar", type="primary", key="co_save"):
        update_company_profile({"name": name, "address_lines": address,
                                 "cif": cif, "phone": phone or None,
                                 "email": email or None, "currency": curr,
                                 "cc_emails": cc.strip() or None})
        st.success("Guardado.")
        st.rerun()


# ─────────────────────────────────────────────────────────────────────
# Google OAuth tab
# ─────────────────────────────────────────────────────────────────────
def _get_app_redirect_uri() -> str:
    """Streamlit Cloud's app URL — override via secret if needed."""
    try:
        v = st.secrets.get("APP_URL")
        if v:
            return str(v).rstrip("/") + "/"
    except Exception:
        pass
    return "https://nyvos-inventory.streamlit.app/"


def _handle_oauth_callback():
    """If Google redirected back with ?code=…, finish the exchange."""
    qp = st.query_params
    code = qp.get("code")
    if not code:
        return
    try:
        exchange_code(code, _get_app_redirect_uri())
        st.toast("✅ Google conectado correctamente.", icon="✅")
    except Exception as exc:
        st.error(f"No se pudo completar la autorización: {exc}")
    # Clear the query and rerun cleanly so the tabs render only once.
    st.query_params.clear()
    st.rerun()


def _render_google():
    tok = get_google_tokens()
    connected = is_connected()

    if connected:
        st.success(f"✅ Conectado como **{tok.get('authorized_email','?')}**")
        st.caption("Permisos: Google Drive (subir PDFs) + Gmail (crear borradores)")
        c1, c2 = st.columns(2)
        if c1.button("🔌 Desconectar Google", key="goog_disconnect"):
            disconnect()
            st.rerun()
        c2.caption("Al desconectar, los botones de Drive/Gmail desaparecen "
                   "hasta que vuelvas a autorizar.")
        return

    st.markdown("""
#### Conectar con tu cuenta de Google Workspace de Monvita

Necesitas hacer esto **una sola vez**. Lleva ~10 minutos. Después, el
dashboard puede subir PDFs a Drive y crear borradores en Gmail por ti.

**Pasos (en otra pestaña del navegador):**

1. Ve a **https://console.cloud.google.com** y accede con `mafe.cubillos@monvita.co`
2. Crea un nuevo proyecto llamado `Monvita Operations` (botón arriba a la izquierda, "New Project")
3. En el buscador de arriba, busca y entra en **"Google Drive API"** → **Enable**
4. Lo mismo con **"Gmail API"** → **Enable**
5. Menú lateral → **APIs & Services → OAuth consent screen**:
   - User type: **Internal** (si tienes Workspace) o **External**
   - App name: `Monvita Operations`
   - User support email: `mafe.cubillos@monvita.co`
   - Developer contact: `mafe.cubillos@monvita.co`
   - **Save and Continue** hasta publicar
6. Menú lateral → **APIs & Services → Credentials → + Create credentials → OAuth client ID**:
   - Application type: **Web application**
   - Name: `Dashboard Streamlit`
   - **Authorized redirect URIs** → añade exactamente:
     ```
     {redirect}
     ```
   - **Create**
7. Copia el **Client ID** y el **Client Secret** que te muestra, y pégalos abajo 👇
""".format(redirect=_get_app_redirect_uri()))

    with st.form("google_client_form"):
        c1, c2 = st.columns(2)
        cid = c1.text_input("Client ID",     value=tok.get("client_id") or "",
                            key="goog_cid")
        csec = c2.text_input("Client Secret", value=tok.get("client_secret") or "",
                             type="password", key="goog_csec")
        submit = st.form_submit_button("💾 Guardar credenciales",
                                        type="primary", use_container_width=True)
        if submit:
            if not cid.strip() or not csec.strip():
                st.error("Faltan Client ID o Client Secret.")
            else:
                save_client(cid, csec)
                st.success("Credenciales guardadas. Ahora pulsa 'Autorizar'.")
                st.rerun()

    if (tok.get("client_id") or "").strip():
        try:
            auth_url = build_auth_url(_get_app_redirect_uri())
            st.markdown(
                f'<a href="{auth_url}" target="_blank" rel="noopener" '
                f'style="display:inline-block;background:#4285F4;color:#fff;'
                f'padding:12px 24px;border-radius:6px;text-decoration:none;'
                f'font-weight:600;margin-top:12px">🔐 Autorizar con Google (nueva pestaña)</a>',
                unsafe_allow_html=True)
            st.caption("Se abre el consentimiento de Google en una nueva pestaña. "
                       "Al aceptar, Google te redirige al dashboard conectado.")
        except Exception as exc:
            st.error(f"Error construyendo URL: {exc}")


# ─────────────────────────────────────────────────────────────────────
# Fase 3: scan Reorder → draft POs → email alert
# ─────────────────────────────────────────────────────────────────────
def _render_auto_scan_banner():
    with st.expander("🤖 Escaneo automático desde Reorder (Fase 3)", expanded=False):
        st.caption(
            "Revisa las sugerencias de Reorder, agrupa por producto del "
            "catálogo (vía ASINs) y crea un draft para cada producto "
            "cuya cantidad sugerida ≥ 70% × MOQ. Después te manda un "
            "email con el resumen.")
        c1, c2 = st.columns([3, 2])
        threshold = c1.slider(
            "Umbral (porcentaje del MOQ para disparar un draft)",
            min_value=10, max_value=100, value=70, step=5,
            key="auto_threshold")
        if c2.button("🔄 Escanear ahora", type="primary",
                     use_container_width=True, key="auto_scan"):
            if not is_connected():
                st.warning("Conecta Google primero (tab 🔗 Google) para "
                           "enviar el email de resumen.")
            with st.spinner("Escaneando reorder_alerts y creando drafts..."):
                drafts = scan_and_create_drafts(threshold_pct=threshold / 100)
            if not drafts:
                st.info("Ningún producto cumple el umbral (o ya tienen "
                        "drafts abiertos). Todo en orden ✨")
            else:
                st.success(f"✅ Creados {len(drafts)} drafts nuevos:")
                for d in drafts:
                    st.markdown(
                        f"- **PO #{d['po']['po_number']}** · {d['product']['name']} · "
                        f"{d['final_qty']:,} ud "
                        f"(sugerido {d['suggested_qty']:,}, MOQ "
                        f"{int(d['product']['moq'])})")
                if is_connected():
                    ok, msg = send_draft_alert_email(drafts)
                    if ok:
                        st.success(f"📧 {msg}")
                    else:
                        st.error(msg)
                st.rerun()
