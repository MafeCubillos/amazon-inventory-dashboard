"""Drive upload + Gmail draft creation for Purchase Orders.

Both functions are no-ops on error (returning a (ok, message) tuple)
rather than raising, so a failed Google call never breaks the PO page.
"""
from __future__ import annotations

import base64
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO

from backend.database import db_admin
from backend.google_oauth import get_credentials
from backend.purchase_orders import get_purchase_order, render_po_pdf


# ─────────────────────────────────────────────────────────────────────
# Drive
# ─────────────────────────────────────────────────────────────────────

def _drive_service():
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=get_credentials(),
                 cache_discovery=False)


def upload_po_to_drive(po_id: int, folder_id: str | None = None
                       ) -> tuple[bool, str, str | None]:
    """Render + upload a PO PDF to Drive.
    If no folder_id is passed, uses the first item's product's folder.
    Returns (ok, message, drive_file_id)."""
    from googleapiclient.http import MediaIoBaseUpload

    po = get_purchase_order(po_id)
    if not po:
        return False, "PO no encontrada.", None

    # Pick folder if not supplied: use first line-item's product folder.
    if not folder_id:
        folder_id = _find_folder_for_po(po)
    if not folder_id:
        return False, ("Este PO no tiene carpeta Drive asignada. "
                       "Añade `drive_folder_id` al producto en el tab 📦 Productos "
                       "o pasa una carpeta por defecto en Configuración."), None

    pdf = render_po_pdf(po)
    filename = f"Orden de compra {po['po_number']}.pdf"
    media = MediaIoBaseUpload(BytesIO(pdf), mimetype="application/pdf",
                              resumable=False)
    try:
        svc = _drive_service()
        file = svc.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media, fields="id,webViewLink",
        ).execute()
    except Exception as exc:
        return False, f"Error subiendo a Drive: {exc}", None

    file_id = file.get("id")
    link    = file.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}"
    db_admin.table("po_orders").update({
        "drive_file_id": file_id,
        "updated_at":    datetime.utcnow().isoformat(),
    }).eq("id", po_id).execute()
    return True, link, file_id


def _find_folder_for_po(po: dict) -> str | None:
    """If the PO's line items map to a known product with a drive_folder_id,
    use that folder. First matching item wins."""
    items = po.get("items") or []
    if not items:
        return None
    # Match by description's first line (product name convention)
    descs = [((it.get("description") or "").splitlines() or [""])[0].strip()
             for it in items]
    if not descs:
        return None
    # Try exact match first
    for d in descs:
        if not d:
            continue
        rows = (db_admin.table("po_products")
                .select("drive_folder_id")
                .eq("name", d)
                .not_.is_("drive_folder_id", "null")
                .limit(1).execute().data) or []
        if rows:
            return rows[0]["drive_folder_id"]
    # Fallback: ilike substring match on the first line
    for d in descs:
        if not d:
            continue
        rows = (db_admin.table("po_products")
                .select("drive_folder_id, name")
                .ilike("name", f"%{d[:30]}%")
                .not_.is_("drive_folder_id", "null")
                .limit(1).execute().data) or []
        if rows:
            return rows[0]["drive_folder_id"]
    return None


# ─────────────────────────────────────────────────────────────────────
# Gmail
# ─────────────────────────────────────────────────────────────────────

_DEFAULT_SUBJECT = "Orden de compra #{po_number} — Monvita Wellness Group"
_DEFAULT_BODY = """Hola {contact},

Te envío adjunto nueva orden de compra:

{items_lines}

¡Mil gracias!
Saludos,

María Fernanda Cubillos
Monvita Wellness Group S.L.
"""


def _item_short_name(desc: str) -> str:
    """First non-empty line of the description — our products put the
    short name there (e.g. 'SAW PALMETTO HAIR')."""
    for ln in (desc or "").splitlines():
        s = ln.strip()
        if s:
            return s
    return "producto"


def create_gmail_draft(po_id: int, to_email: str | None = None,
                       subject: str | None = None,
                       body: str | None = None
                       ) -> tuple[bool, str, str | None]:
    """Create a draft email in María's Gmail with the PO PDF attached.
    Returns (ok, message, draft_id)."""
    po = get_purchase_order(po_id)
    if not po:
        return False, "PO no encontrada.", None

    sup = po.get("supplier") or {}
    to_email = (to_email or sup.get("email") or "").strip()
    if not to_email:
        return False, (f"El proveedor '{sup.get('name','?')}' no tiene email. "
                       "Añádelo en el tab 🏢 Proveedores."), None

    items = po.get("items") or []
    items_lines = "\n".join(
        f"- OC {po['po_number']}: {_item_short_name(it.get('description',''))} "
        f"({int(float(it['quantity'])) if float(it['quantity']).is_integer() else it['quantity']} unid)"
        for it in items
    ) or f"- OC {po['po_number']}"

    contact_name = (sup.get("contact_name") or "").strip() or "equipo"
    subject = subject or _DEFAULT_SUBJECT.format(po_number=po["po_number"])
    body    = body    or _DEFAULT_BODY.format(contact=contact_name,
                                              items_lines=items_lines)

    # Build MIME message with PDF attachment
    msg = MIMEMultipart()
    msg["to"]      = to_email
    msg["subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    pdf = render_po_pdf(po)
    attach = MIMEApplication(pdf, _subtype="pdf")
    attach.add_header("Content-Disposition", "attachment",
                      filename=f"Orden de compra {po['po_number']}.pdf")
    msg.attach(attach)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    try:
        from googleapiclient.discovery import build
        svc = build("gmail", "v1", credentials=get_credentials(),
                    cache_discovery=False)
        draft = svc.users().drafts().create(
            userId="me", body={"message": {"raw": raw}}
        ).execute()
    except Exception as exc:
        return False, f"Error creando draft en Gmail: {exc}", None

    draft_id = draft.get("id")
    db_admin.table("po_orders").update({
        "gmail_draft_id": draft_id,
        "updated_at":     datetime.utcnow().isoformat(),
    }).eq("id", po_id).execute()
    return True, "Borrador creado en Gmail.", draft_id
