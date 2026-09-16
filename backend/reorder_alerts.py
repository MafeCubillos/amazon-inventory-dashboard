"""Reorder Planner alerts — Mon/Thu email showing per-ASIN reorder needs
that require a supplier PO in the next N days.

Sends alongside the existing inventory alert (backend/alerts.py) but with a
separate table + focus: WHEN to place the next supplier order, not which
markets are running low right now.

Setup uses the same SMTP env vars as backend/alerts.py:
    ALERT_EMAIL_TO / ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD.
Add an optional REORDER_EMAIL_TO to route this one to a different / larger
recipient list (e.g. include Carlos); otherwise falls back to ALERT_EMAIL_TO.
"""

from __future__ import annotations

import logging
import math as _math
import os
import smtplib
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from backend.database import db_admin

logger = logging.getLogger(__name__)

# Only include ASINs whose next PO deadline is within this many days.
REORDER_WINDOW_DAYS = 90


# ── Data loader ────────────────────────────────────────────────────────────

def _load_reorder_data() -> list[dict]:
    """Compute per-ASIN reorder needs (mirrors dashboard's Reorder Planner).
    Returns rows with keys: asin, name, days_to_order, order_by_dt,
    reorder_qty, stockout_lbl, alert (🔴/🟡/🟠/🔵), source ('forecast'|'velocity').
    Sorted by urgency (soonest deadline first).
    """
    today = date.today()

    # Product master
    prods = db_admin.table("products").select("*").execute().data or []
    products = {p["asin"]: p for p in prods}

    # Latest inventory per (asin, marketplace) — server-side desc sort so the
    # newest snapshot survives the 1000-row REST limit.
    inv_rows_raw = (
        db_admin.table("inventory_snapshots")
        .select("asin,marketplace,units_available,units_inbound,snapshot_date")
        .order("snapshot_date", desc=True)
        .execute()
        .data
        or []
    )
    latest_inv: dict[tuple, dict] = {}
    for r in inv_rows_raw:
        key = (r["asin"], r["marketplace"])
        if key not in latest_inv:
            latest_inv[key] = r

    # Latest velocity per (asin, marketplace)
    vel_rows_raw = (
        db_admin.table("sales_velocity")
        .select("asin,marketplace,velocity_daily,period_end_date")
        .order("period_end_date", desc=True)
        .execute()
        .data
        or []
    )
    latest_vel: dict[tuple, float] = {}
    for r in vel_rows_raw:
        key = (r["asin"], r["marketplace"])
        if key not in latest_vel:
            latest_vel[key] = float(r.get("velocity_daily") or 0)

    # Aggregate stock/inbound/velocity per ASIN
    agg: dict[str, dict] = {}
    for (asin, mp), inv in latest_inv.items():
        d = agg.setdefault(asin, {"stock": 0, "inbound": 0, "vel": 0.0})
        d["stock"]   += int(inv.get("units_available") or 0)
        d["inbound"] += int(inv.get("units_inbound")   or 0)
        d["vel"]     += latest_vel.get((asin, mp), 0.0)

    # Open POs (on order from supplier, not yet at FBA)
    try:
        po_rows = db_admin.table("purchase_orders").select(
            "asin,units_ordered,status"
        ).in_("status", ["ordered", "shipped"]).execute().data or []
    except Exception:
        po_rows = []
    on_order: dict[str, int] = {}
    for po in po_rows:
        on_order[po["asin"]] = on_order.get(po["asin"], 0) + int(po.get("units_ordered") or 0)

    # Local + TikTok stock (both reduce supplier need)
    try:
        local_rows = db_admin.table("local_stock").select("asin,units").execute().data or []
        local = {r["asin"]: int(r.get("units") or 0) for r in local_rows}
    except Exception:
        local = {}
    try:
        tt_rows = db_admin.table("tiktok_stock").select("asin,units").execute().data or []
        tt = {r["asin"]: int(r.get("units") or 0) for r in tt_rows}
    except Exception:
        tt = {}

    # Forecast (optional)
    try:
        from backend.fetchers.forecast import fetch_forecast
        fcast_data = fetch_forecast() or {}
    except Exception:
        fcast_data = {}

    def _mk(offset: int) -> str:
        y = today.year + (today.month - 1 + offset) // 12
        m = (today.month - 1 + offset) % 12 + 1
        return f"{y:04d}-{m:02d}"

    out: list[dict] = []
    for asin, p in products.items():
        a       = agg.get(asin, {"stock": 0, "inbound": 0, "vel": 0.0})
        stock   = a["stock"]
        inbound = a["inbound"]
        vel     = a["vel"]
        oo      = on_order.get(asin, 0)
        loc     = local.get(asin, 0)
        tts     = tt.get(asin, 0)
        lead    = int(p.get("lead_time_days")       or 30)
        target  = int(p.get("target_days_coverage") or 60)

        fc_info = fcast_data.get(asin) or {}
        fc_df   = fc_info.get("data") if isinstance(fc_info, dict) else None

        if fc_df is not None and not fc_df.empty:
            fc_cols  = list(fc_df.columns)
            # Walk months, subtracting demand from all supply
            remaining   = stock + inbound + oo + loc + tts
            stockout_ym = None
            for offset in range(20):
                mk = _mk(offset)
                if mk not in fc_df.index:
                    break
                month_demand = sum(int(fc_df.loc[mk, mp]) for mp in fc_cols)
                remaining -= month_demand
                if remaining < 0:
                    stockout_ym = mk
                    break
            if stockout_ym:
                stockout_dt   = datetime.strptime(stockout_ym, "%Y-%m").date()
                order_by_dt   = stockout_dt - timedelta(days=lead)
                days_to_order = (order_by_dt - today).days
                stockout_lbl  = stockout_dt.strftime("%b %Y")
            else:
                order_by_dt   = None
                days_to_order = 9999
                stockout_lbl  = "OK through forecast"
            months_ahead = max(2, _math.ceil((lead + target) / 30))
            fc_window    = sum(
                int(fc_df.loc[_mk(i), mp])
                for i in range(months_ahead)
                for mp in fc_cols
                if _mk(i) in fc_df.index
            )
            reorder_qty = max(0, fc_window - stock - inbound - oo - loc)
            source      = "forecast"
        else:
            # Velocity fallback
            if stock == 0 and vel == 0:
                continue
            supply = stock + inbound + oo + loc + tts
            days_left = supply / vel if vel > 0 else 9999
            days_to_order = int(round(days_left - lead)) if days_left < 9999 else 9999
            order_by_dt   = (today + timedelta(days=max(0, days_to_order))) \
                             if days_to_order < 9999 else None
            reorder_qty   = max(0, int((target - days_left) * vel)) if vel > 0 else 0
            stockout_lbl  = f"{int(days_left)}d left" if days_left < 9999 else "—"
            source        = "velocity"

        # Filter to actionable window
        if days_to_order > REORDER_WINDOW_DAYS:
            continue
        if reorder_qty <= 0:
            continue

        # Alert bucket (matches dashboard)
        if days_to_order <= 0:
            alert = "critical"
        elif days_to_order <= 14:
            alert = "warning"
        elif days_to_order <= 45:
            alert = "upcoming"
        else:
            alert = "plan"

        out.append({
            "asin":          asin,
            "name":          p.get("product_name") or asin,
            "days_to_order": days_to_order,
            "order_by":      order_by_dt.strftime("%d %b %Y") if order_by_dt else "—",
            "reorder_qty":   reorder_qty,
            "stockout":      stockout_lbl,
            "alert":         alert,
            "source":        source,
        })

    # Sort by urgency (soonest deadline first)
    out.sort(key=lambda r: r["days_to_order"])
    return out


# ── HTML builder ───────────────────────────────────────────────────────────

_ALERT_STYLE = {
    "critical": ("🔴 Order NOW",  "#FCEBEB", "#A32D2D", "#FFF5F5"),
    "warning":  ("🟡 Order soon", "#FAEEDA", "#854F0B", "#FFFBF0"),
    "upcoming": ("🟠 Upcoming",   "#FFE8D6", "#8A4B00", "#FFFAF3"),
    "plan":     ("🔵 Plan ahead", "#DEEAFB", "#123B7A", "#F6F9FF"),
}


def build_reorder_html(rows: list[dict]) -> str:
    today   = date.today().strftime("%A, %d %b %Y")
    n_crit  = sum(1 for r in rows if r["alert"] == "critical")
    n_warn  = sum(1 for r in rows if r["alert"] == "warning")
    n_up    = sum(1 for r in rows if r["alert"] == "upcoming")
    n_plan  = sum(1 for r in rows if r["alert"] == "plan")

    def _badge(a):
        label, bg, fg, _ = _ALERT_STYLE.get(a, ("—", "#EEE", "#666", "#FAFAFA"))
        return (f'<span style="background:{bg};color:{fg};padding:2px 8px;'
                f'border-radius:12px;font-size:12px;font-weight:700">{label}</span>')

    def _days_color(a):
        return {"critical": "#E24B4A", "warning": "#BA7517",
                "upcoming": "#B36400", "plan": "#1A56DB"}.get(a, "#666")

    rows_html = ""
    for r in rows:
        _, _, _, bg = _ALERT_STYLE.get(r["alert"], ("—", "#EEE", "#666", "#FAFAFA"))
        dto = r["days_to_order"]
        dto_lbl = f"{dto}d" if dto > 0 else "TODAY"
        rows_html += f"""
<tr style="background:{bg};border-bottom:1px solid #F0E8E8">
  <td style="padding:10px 14px;font-size:13px;font-weight:600;color:#111">{r['name'][:55]}</td>
  <td style="padding:10px 14px;font-size:13px;text-align:center">{_badge(r['alert'])}</td>
  <td style="padding:10px 14px;font-size:14px;font-weight:700;color:{_days_color(r['alert'])};text-align:center">{dto_lbl}</td>
  <td style="padding:10px 14px;font-size:13px;text-align:center;color:#555">{r['order_by']}</td>
  <td style="padding:10px 14px;font-size:13px;text-align:right;color:#111;font-weight:700">{r['reorder_qty']:,}</td>
  <td style="padding:10px 14px;font-size:12px;text-align:center;color:#888">{r['stockout']}</td>
</tr>"""

    if not rows:
        rows_html = """
<tr><td colspan="6" style="padding:32px;text-align:center;color:#888;font-size:14px">
  ✅ Nothing to reorder in the next 90 days — you're all set!
</td></tr>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#F4F4F2;font-family:-apple-system,BlinkMacSystemFont,'Inter',sans-serif">
<div style="max-width:760px;margin:32px auto;background:#fff;border-radius:12px;
            overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08)">

  <!-- Header -->
  <div style="background:#111;padding:24px 32px">
    <div style="color:#C8FF00;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase">Nyvos EU Dashboard</div>
    <div style="color:#fff;font-size:22px;font-weight:800;margin-top:4px">🚚 Reorder Planner Alert</div>
    <div style="color:#888;font-size:12px;margin-top:4px">{today}</div>
  </div>

  <!-- Summary -->
  <div style="display:flex;gap:0;border-bottom:1px solid #EEE">
    <div style="flex:1;padding:20px 16px;text-align:center;border-right:1px solid #EEE">
      <div style="font-size:28px;font-weight:800;color:#E24B4A">{n_crit}</div>
      <div style="font-size:11px;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:.05em">🔴 Now</div>
    </div>
    <div style="flex:1;padding:20px 16px;text-align:center;border-right:1px solid #EEE">
      <div style="font-size:28px;font-weight:800;color:#BA7517">{n_warn}</div>
      <div style="font-size:11px;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:.05em">🟡 Soon (≤14d)</div>
    </div>
    <div style="flex:1;padding:20px 16px;text-align:center;border-right:1px solid #EEE">
      <div style="font-size:28px;font-weight:800;color:#B36400">{n_up}</div>
      <div style="font-size:11px;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:.05em">🟠 Upcoming (≤45d)</div>
    </div>
    <div style="flex:1;padding:20px 16px;text-align:center">
      <div style="font-size:28px;font-weight:800;color:#1A56DB">{n_plan}</div>
      <div style="font-size:11px;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:.05em">🔵 Plan (≤90d)</div>
    </div>
  </div>

  <!-- Intro copy -->
  <div style="padding:16px 32px;background:#FAFAF8;border-bottom:1px solid #EEE;color:#555;font-size:13px;line-height:1.5">
    Products below need a supplier PO in the next 90 days. Sorted by urgency —
    place orders for 🔴 rows immediately. See the full Reorder Planner tab for
    per-country breakdowns and local/TikTok stock offsets.
  </div>

  <!-- Table -->
  <div>
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="background:#F8F8F6">
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.05em">Product</th>
          <th style="padding:10px 14px;text-align:center;font-size:11px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.05em">Alert</th>
          <th style="padding:10px 14px;text-align:center;font-size:11px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.05em">Days to order</th>
          <th style="padding:10px 14px;text-align:center;font-size:11px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.05em">Order by</th>
          <th style="padding:10px 14px;text-align:right;font-size:11px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.05em">Qty</th>
          <th style="padding:10px 14px;text-align:center;font-size:11px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.05em">Stockout est.</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>

  <!-- Footer -->
  <div style="padding:20px 32px;background:#F8F8F6;border-top:1px solid #EEE;text-align:center">
    <a href="https://nyvos-inventory.streamlit.app/?page=reorder" style="color:#1A56DB;text-decoration:none;font-size:13px;font-weight:600">
      🚚 Open Reorder Planner →
    </a>
    <div style="font-size:12px;color:#999;margin-top:8px">
      Sent automatically every Monday & Thursday · Nyvos EU Dashboard
    </div>
  </div>

</div>
</body></html>"""


# ── Send ───────────────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    return s.replace("\r", "").replace("\n", "").strip()


def send_reorder_email() -> bool:
    """Compute reorder needs → build email → send via SMTP."""
    smtp_from  = _clean(os.getenv("ALERT_EMAIL_FROM", ""))
    smtp_pass  = _clean(os.getenv("ALERT_EMAIL_PASSWORD", ""))
    to_list    = _clean(os.getenv("REORDER_EMAIL_TO", "") or os.getenv("ALERT_EMAIL_TO", ""))
    if not (smtp_from and smtp_pass and to_list):
        logger.error("reorder_email  missing SMTP env vars")
        return False

    rows = _load_reorder_data()
    # Only skip send if truly nothing to report (0 rows across all buckets)
    logger.info("reorder_email  %d ASINs in reorder window", len(rows))

    n_crit = sum(1 for r in rows if r["alert"] == "critical")
    n_warn = sum(1 for r in rows if r["alert"] == "warning")
    subject_bits = []
    if n_crit: subject_bits.append(f"{n_crit} now")
    if n_warn: subject_bits.append(f"{n_warn} soon")
    subject_tag = f" — {', '.join(subject_bits)}" if subject_bits else ""
    subject = f"🚚 Nyvos Reorder Alert{subject_tag}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Nyvos Dashboard <{smtp_from}>"
    msg["To"]      = to_list
    msg.attach(MIMEText(build_reorder_html(rows), "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(smtp_from, smtp_pass)
            s.send_message(msg)
        logger.info("reorder_email  sent to %s (%d rows)", to_list, len(rows))
        return True
    except Exception as exc:
        logger.error("reorder_email  send failed: %s", exc)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ok = send_reorder_email()
    print("✅ sent" if ok else "❌ failed")
