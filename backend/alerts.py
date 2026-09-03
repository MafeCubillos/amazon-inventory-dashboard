"""Weekly inventory alert emails — sent Monday & Thursday at 8:00 AM UTC.

Setup in .env:
    ALERT_EMAIL_TO=you@example.com          # recipient(s), comma-separated
    ALERT_EMAIL_FROM=sender@gmail.com       # Gmail address used to send
    ALERT_EMAIL_PASSWORD=xxxx xxxx xxxx     # Gmail App Password (16-char)
    ALERT_EMAIL_HOUR=8                      # optional, default 8 (UTC)

Gmail App Password:
    Google Account → Security → 2-Step Verification → App passwords
    → Select app: Mail  → Generate → copy the 16-char code
"""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from backend.database import db_admin

logger = logging.getLogger(__name__)

FLAGS = {"ES": "🇪🇸", "FR": "🇫🇷", "DE": "🇩🇪", "IT": "🇮🇹",
         "NL": "🇳🇱", "BE": "🇧🇪", "IE": "🇮🇪", "TT": "🎵"}


# ── Data helpers ───────────────────────────────────────────────────────────

def _load_alert_data() -> list[dict]:
    """Return products with critical or warning status from Supabase.

    Recomputes alerts fresh against the latest inventory + velocity so the
    email doesn't leak stale reorder_alerts rows (e.g. a critical status
    from a sync when stock was low, still surfaced after the user restocked).
    Also skips ASINs whose products row is missing (cleaned-up SKUs) to
    avoid emailing about products that no longer exist.
    """
    try:
        # Always recompute before reading — cheap Supabase-only operation
        try:
            from backend.reorder import calculate_reorder_alerts
            calculate_reorder_alerts()
        except Exception as exc:
            logger.warning("alerts  could not recompute reorder alerts: %s", exc)

        products = {
            r["asin"]: r
            for r in (db_admin.table("products").select(
                "asin,product_name,lead_time_days"
            ).execute().data or [])
        }

        alerts = db_admin.table("reorder_alerts").select(
            "asin,marketplace,alert_status,days_of_stock_left,suggested_reorder_qty"
        ).in_("alert_status", ["critical", "warning"]).execute().data or []

        rows = []
        for a in alerts:
            asin = a["asin"]
            # Skip alerts for ASINs no longer in the products catalog (e.g.
            # delisted / renamed SKUs). Their reorder_alerts rows are orphans.
            if asin not in products:
                continue
            reorder_qty = int(a.get("suggested_reorder_qty") or 0)
            # Skip alerts with no actionable reorder quantity — status can flip
            # to "critical" when days_of_stock_left < lead_time even though
            # buffer coverage is already met, producing noisy "critical, order 0"
            # rows that only confuse the reader.
            if reorder_qty <= 0:
                continue
            p = products[asin]
            rows.append({
                "asin":        asin,
                "name":        p.get("product_name") or asin,
                "mp":          a["marketplace"],
                "flag":        FLAGS.get(a["marketplace"], ""),
                "status":      a["alert_status"],
                "days_left":   round(float(a.get("days_of_stock_left") or 0), 0),
                "reorder_qty": reorder_qty,
                "lead":        int(p.get("lead_time_days") or 30),
            })

        # ── TikTok alerts (added as a 5th "marketplace" TT) ──────────
        # Amazon reorder_alerts only cover ES/FR/DE/IT (from FBA inventory).
        # For TT we compute alerts on the fly: units come from tiktok_stock
        # (either manual or auto-synced from TikTok Shop API), velocity from
        # the TT column of the Google Sheets forecast.
        try:
            tt_rows = _load_tiktok_alerts(products)
            rows.extend(tt_rows)
        except Exception as exc:
            logger.warning("alerts  tiktok section failed: %s", exc)

        # Sort: critical first, then by days_left ascending
        rows.sort(key=lambda x: (0 if x["status"] == "critical" else 1, x["days_left"]))
        return rows

    except Exception as exc:
        logger.error("alerts  failed to load data: %s", exc)
        return []


def _load_tiktok_alerts(products: dict[str, dict]) -> list[dict]:
    """Compute TikTok alerts by combining tiktok_stock (units) with the
    Google Sheets forecast (velocity). Returns rows with mp='TT' so they
    fit into the same email table as Amazon markets."""
    from datetime import date as _date

    # 1. Current TikTok stock per ASIN
    tt_rows = db_admin.table("tiktok_stock").select(
        "asin,units"
    ).execute().data or []
    tt_units = {r["asin"]: int(r.get("units") or 0) for r in tt_rows}
    if not tt_units:
        return []

    # 2. TikTok velocity from forecast (current month TT ÷ 30)
    today = _date.today()
    cur_ym = today.strftime("%Y-%m")
    tt_vel: dict[str, float] = {}
    try:
        from backend.fetchers.forecast import fetch_forecast
        fcst = fetch_forecast()
    except Exception:
        fcst = {}

    for asin, info in (fcst or {}).items():
        if not isinstance(info, dict):
            continue
        df = info.get("data")
        if df is None or "TT" not in df.columns or cur_ym not in df.index:
            continue
        try:
            monthly = float(df.loc[cur_ym, "TT"])
            tt_vel[asin] = monthly / 30.0
        except Exception:
            continue

    # 3. Build alert rows
    out: list[dict] = []
    for asin, units in tt_units.items():
        if asin not in products:
            continue
        vel = tt_vel.get(asin, 0.0)
        if vel <= 0:
            continue   # no forecast → can't compute alert
        p        = products[asin]
        lead     = int(p.get("lead_time_days") or 30)
        days_left = round(units / vel, 1) if vel > 0 else 9999
        # Same status thresholds as reorder module
        if days_left < lead:
            status = "critical"
        elif days_left < (lead + 15):
            status = "warning"
        else:
            continue   # ok → skip
        # Reorder qty to reach target coverage
        target      = 60  # same default as reorder module
        reorder_qty = max(0, int(round((target - days_left) * vel)))
        if reorder_qty <= 0:
            continue
        out.append({
            "asin":        asin,
            "name":        p.get("product_name") or asin,
            "mp":          "TT",
            "flag":        FLAGS.get("TT", "🎵"),
            "status":      status,
            "days_left":   round(days_left, 0),
            "reorder_qty": reorder_qty,
            "lead":        lead,
        })
    return out


# ── Email builder ──────────────────────────────────────────────────────────

def _build_html(rows: list[dict]) -> str:
    today   = date.today().strftime("%A, %d %b %Y")
    n_crit  = sum(1 for r in rows if r["status"] == "critical")
    n_warn  = sum(1 for r in rows if r["status"] == "warning")

    def _status_badge(s):
        if s == "critical":
            return '<span style="background:#FCEBEB;color:#A32D2D;padding:2px 8px;border-radius:12px;font-size:12px;font-weight:700">🔴 Critical</span>'
        return '<span style="background:#FAEEDA;color:#854F0B;padding:2px 8px;border-radius:12px;font-size:12px;font-weight:700">🟡 Warning</span>'

    # Color the days number by STATUS, not by absolute thresholds, so a
    # "Critical" row never shows a green day count (which read as a bug).
    def _days_color(status):
        return {"critical": "#E24B4A", "warning": "#BA7517"}.get(status, "#3B6D11")

    rows_html = ""
    for r in rows:
        bg = "#FFF5F5" if r["status"] == "critical" else "#FFFBF0"
        rows_html += f"""
<tr style="background:{bg};border-bottom:1px solid #F0E8E8">
  <td style="padding:10px 14px;font-size:13px;font-weight:600;color:#111">{r['name'][:55]}</td>
  <td style="padding:10px 14px;font-size:13px;text-align:center">{r['flag']} {r['mp']}</td>
  <td style="padding:10px 14px;font-size:13px;text-align:center">{_status_badge(r['status'])}</td>
  <td style="padding:10px 14px;font-size:14px;font-weight:700;color:{_days_color(r['status'])};text-align:center">{int(r['days_left'])}d</td>
  <td style="padding:10px 14px;font-size:13px;text-align:center;color:#555">{r['reorder_qty']:,}</td>
</tr>"""

    # Optional correction / notice banner passed via ALERT_EMAIL_NOTE env var.
    # Rendered at the top of the email between the header and the summary
    # cards. Useful for one-off correction sends ("the previous email had
    # wrong data") without touching the schedule.
    note = os.getenv("ALERT_EMAIL_NOTE", "").strip()
    note_html = (
        f"""
  <div style="background:#FFF4E5;border-bottom:1px solid #F0D9B0;padding:14px 24px;
              color:#7A4F00;font-size:13px;font-weight:600">
    {note}
  </div>"""
        if note
        else ""
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#F4F4F2;font-family:-apple-system,BlinkMacSystemFont,'Inter',sans-serif">
<div style="max-width:680px;margin:32px auto;background:#fff;border-radius:12px;
            overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08)">

  <!-- Header -->
  <div style="background:#111;padding:24px 32px;display:flex;align-items:center;gap:16px">
    <div>
      <div style="color:#C8FF00;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase">Nyvos · Amazon EU Dashboard</div>
      <div style="color:#fff;font-size:22px;font-weight:800;margin-top:4px">📦 Inventory Alert</div>
      <div style="color:#888;font-size:12px;margin-top:4px">{today}</div>
    </div>
  </div>
{note_html}
  <!-- Summary -->
  <div style="display:flex;gap:0;border-bottom:1px solid #EEE">
    <div style="flex:1;padding:20px 24px;text-align:center;border-right:1px solid #EEE">
      <div style="font-size:32px;font-weight:800;color:#E24B4A">{n_crit}</div>
      <div style="font-size:12px;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Critical</div>
    </div>
    <div style="flex:1;padding:20px 24px;text-align:center;border-right:1px solid #EEE">
      <div style="font-size:32px;font-weight:800;color:#BA7517">{n_warn}</div>
      <div style="font-size:12px;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Warning</div>
    </div>
    <div style="flex:1;padding:20px 24px;text-align:center">
      <div style="font-size:32px;font-weight:800;color:#111">{n_crit + n_warn}</div>
      <div style="font-size:12px;color:#888;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Total alerts</div>
    </div>
  </div>

  <!-- Table -->
  <div style="padding:0">
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="background:#F8F8F6">
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.05em">Product</th>
          <th style="padding:10px 14px;text-align:center;font-size:11px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.05em">Market</th>
          <th style="padding:10px 14px;text-align:center;font-size:11px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.05em">Status</th>
          <th style="padding:10px 14px;text-align:center;font-size:11px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.05em">Days left</th>
          <th style="padding:10px 14px;text-align:center;font-size:11px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.05em">Reorder qty</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>

  <!-- Footer -->
  <div style="padding:20px 32px;background:#F8F8F6;border-top:1px solid #EEE;text-align:center">
    <div style="font-size:12px;color:#999">
      Sent automatically every Monday & Thursday · Nyvos Amazon EU Dashboard
    </div>
  </div>

</div>
</body></html>"""


# ── Send ───────────────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    """Remove embedded newlines/CR that break email headers."""
    return s.replace("\r", "").replace("\n", "").strip()


def send_alert_email() -> bool:
    """Build and send the alert email. Returns True on success."""
    smtp_from = _clean(os.getenv("ALERT_EMAIL_FROM", ""))
    smtp_pass = _clean(os.getenv("ALERT_EMAIL_PASSWORD", ""))
    smtp_to   = [_clean(t) for t in os.getenv("ALERT_EMAIL_TO", "").replace("\n", ",").split(",") if _clean(t)]

    if not (smtp_from and smtp_pass and smtp_to):
        logger.warning("alerts  email not configured — set ALERT_EMAIL_FROM/PASSWORD/TO in .env")
        return False

    rows = _load_alert_data()
    if not rows:
        logger.info("alerts  no critical/warning products — skipping email")
        return True   # nothing to send, but not an error

    n_crit = sum(1 for r in rows if r["status"] == "critical")
    n_warn = sum(1 for r in rows if r["status"] == "warning")
    subject = f"⚠️ Nyvos Inventory Alert — {n_crit} critical, {n_warn} warning"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Nyvos Dashboard <{smtp_from}>"
    msg["To"]      = ", ".join(smtp_to)
    msg.attach(MIMEText(_build_html(rows), "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(smtp_from, smtp_pass)
            smtp.sendmail(smtp_from, smtp_to, msg.as_string())
        logger.info("alerts  email sent → %s  (%d alerts)", smtp_to, len(rows))
        return True
    except Exception as exc:
        logger.error("alerts  email failed: %s", exc)
        return False


# ── Quick test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    ok = send_alert_email()
    sys.exit(0 if ok else 1)
