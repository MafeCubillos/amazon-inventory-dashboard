"""Nyvos Amazon EU Inventory Dashboard — v2.0"""

import sys
import base64
import io
import os
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure project root is on the path so `backend` is always importable
# regardless of the working directory Streamlit is launched from
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import pandas as pd
import streamlit as st

# ── Load secrets into os.environ (Streamlit Cloud uses st.secrets) ──────────
try:
    for _k, _v in st.secrets.to_dict().items():
        if isinstance(_v, str):
            os.environ[_k] = _v   # cloud secrets always take priority
except Exception:
    pass  # local dev uses .env via load_dotenv()
import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv()

# ── Logo (base64 for HTML embedding) ──────────────────────────
def _logo_b64() -> str:
    logo_path = Path(__file__).parent / "assets" / "logo_white.png"
    if logo_path.exists():
        return base64.b64encode(logo_path.read_bytes()).decode()
    return ""

LOGO_B64 = _logo_b64()
LOGO_IMG = (f'<img src="data:image/png;base64,{LOGO_B64}" '
            f'style="height:44px;display:block" alt="Nyvos">'
            if LOGO_B64 else
            '<span style="font-size:22px;font-weight:900;color:#fff;letter-spacing:.08em">NYVOS</span>')

LOGO_IMG_SM = (f'<img src="data:image/png;base64,{LOGO_B64}" '
               f'style="height:36px;display:block;margin-bottom:6px" alt="Nyvos">'
               if LOGO_B64 else
               '<span style="font-size:20px;font-weight:900;color:#fff">NYVOS</span>')

# ── Page config ────────────────────────────────────────────────
st.set_page_config(
    page_title="Nyvos · Inventory",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Demo mode ──────────────────────────────────────────────────
_SB_URL = os.getenv("SUPABASE_URL", "")
_SB_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")   # bypasses RLS — preferred for internal dashboard
    or os.getenv("SUPABASE_ANON_KEY", "")
)
DEMO_MODE = not (_SB_URL and _SB_KEY)

db = None
if not DEMO_MODE:
    from supabase import create_client
    @st.cache_resource
    def _get_db():
        return create_client(_SB_URL, _SB_KEY)
    db = _get_db()

MARKETPLACES = ["ES", "FR", "DE", "IT"]
FLAGS        = {"ES": "🇪🇸", "FR": "🇫🇷", "DE": "🇩🇪", "IT": "🇮🇹"}
COUNTRY_NAMES = {"ES": "Spain", "FR": "France", "DE": "Germany", "IT": "Italy"}


# ══════════════════════════════════════════════════════════════
# CSS
# ══════════════════════════════════════════════════════════════

def inject_css():
    st.markdown("""
<style>
:root {
    --accent: #C8FF00;
    --black:  #111111;
    --bg:     #F4F4F2;
    --white:  #FFFFFF;
    --crit-bg: #FCEBEB; --crit-fg: #A32D2D;
    --warn-bg: #FAEEDA; --warn-fg: #854F0B;
    --ok-bg:   #EAF3DE; --ok-fg:   #3B6D11;
    --days-red:   #E24B4A;
    --days-amber: #BA7517;
    --days-green: #639922;
}
/* ── Global ────────────────── */
.stApp { background-color: var(--bg) !important; }
header[data-testid="stHeader"] { display: none !important; }
.block-container {
    padding-top: 0.75rem !important;
    padding-left: 2rem !important;
    padding-right: 2rem !important;
    max-width: 100% !important;
}
/* ── Sidebar — force always visible ── */
section[data-testid="stSidebar"],
section[data-testid="stSidebar"][aria-expanded="false"],
section[data-testid="stSidebar"][aria-expanded="true"] {
    background-color: var(--black) !important;
    transform: translateX(0) !important;
    display: block !important;
    min-width: 244px !important;
    width: 21rem !important;
    visibility: visible !important;
    opacity: 1 !important;
    pointer-events: all !important;
    position: relative !important;
    left: 0 !important;
}
section[data-testid="stSidebar"] > div,
[data-testid="stSidebarContent"] {
    display: block !important;
    visibility: visible !important;
    opacity: 1 !important;
    transform: none !important;
}
section[data-testid="stSidebar"] .stMarkdown *,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] span { color: #ccc !important; }
[data-testid="stSidebarNav"] { display: none; }
/* Hide collapse button — sidebar is always visible, can never be hidden */
[data-testid="stSidebarHeader"] { display: none !important; }
/* ── Sidebar nav — active button ─── */
section[data-testid="stSidebar"] button[data-testid="stBaseButton-primary"] {
    background: #1A1A1A !important;
    border-left: 3px solid #C8FF00 !important;
    border-top: none !important;
    border-right: none !important;
    border-bottom: none !important;
    border-radius: 0 6px 6px 0 !important;
    color: #C8FF00 !important;
    font-weight: 700 !important;
    font-size: 13px !important;
    text-align: left !important;
    padding: 10px 16px !important;
    margin: 2px 0 !important;
    box-shadow: none !important;
}
section[data-testid="stSidebar"] button[data-testid="stBaseButton-primary"]:hover {
    background: #222 !important;
}
/* ── Sidebar nav — inactive button ─── */
section[data-testid="stSidebar"] button[data-testid="stBaseButton-secondary"] {
    background: transparent !important;
    border: none !important;
    border-left: 3px solid transparent !important;
    border-radius: 0 6px 6px 0 !important;
    color: #777 !important;
    font-size: 13px !important;
    text-align: left !important;
    padding: 10px 16px !important;
    margin: 2px 0 !important;
    box-shadow: none !important;
}
section[data-testid="stSidebar"] button[data-testid="stBaseButton-secondary"]:hover {
    background: #1A1A1A !important;
    color: #ccc !important;
    border-left: 3px solid #444 !important;
}
/* ── Primary button → accent ─ */
button[data-testid="baseButton-primary"] {
    background: var(--accent) !important;
    color: var(--black) !important;
    font-weight: 700 !important;
    border: none !important;
    border-radius: 6px !important;
}
button[data-testid="baseButton-primary"]:hover {
    background: #b8ef00 !important;
}
/* ── Tabs ──────────────────── */
.stTabs [data-baseweb="tab-list"] { gap: 4px; background: transparent; }
.stTabs [data-baseweb="tab"] {
    border-radius: 6px 6px 0 0;
    font-weight: 600; font-size: 14px; color: #888;
    padding: 8px 20px;
}
.stTabs [aria-selected="true"] {
    background: white;
    color: #111 !important;
    border-bottom: 3px solid var(--accent) !important;
}
/* ── Marketplace cards ─────── */
.mp-card {
    background: #fff;
    border-radius: 10px;
    border: 0.5px solid #E0E0E0;
    border-top: 3px solid #C8FF00;
    padding: 18px 20px;
    transition: box-shadow .2s, transform .2s;
}
.mp-card:hover {
    box-shadow: 0 4px 16px rgba(0,0,0,.1);
    transform: translateY(-2px);
}
/* ── Expander ──────────────── */
.streamlit-expanderHeader {
    background: white !important;
    border-radius: 8px !important;
    font-weight: 600 !important;
}
/* ── Topbar buttons row ─────── */
.topbar-btn-row button {
    height: 36px !important;
    min-height: 36px !important;
    padding: 0 12px !important;
    font-size: 13px !important;
}
/* ── Multiselect pill colors ── */
[data-baseweb="tag"] {
    background-color: #2D3748 !important;
    border-color: #2D3748 !important;
    border-radius: 5px !important;
}
[data-baseweb="tag"] span {
    color: #FFFFFF !important;
    font-weight: 600 !important;
    font-size: 12px !important;
}
[data-baseweb="tag"] button svg {
    fill: #aaa !important;
}
[data-baseweb="tag"] button:hover svg {
    fill: #fff !important;
}
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
# DEMO DATA
# ══════════════════════════════════════════════════════════════

_DEMO_PRODUCTS = [
    ("B0CNYV1234", "NYVOS-OM3-60",  "Omega-3 Fish Oil 1000mg · 60 caps",     8.40, 30, 60),
    ("B0CNYV2345", "NYVOS-VD3-90",  "Vitamin D3 + K2 · 90 caps",             5.20, 25, 60),
    ("B0CNYV3456", "NYVOS-MAG-120", "Magnesium Glycinate 400mg · 120 caps",   9.10, 30, 75),
    ("B0CNYV4567", "NYVOS-COL-30",  "Collagen Peptides Type I & III · 30sv", 12.60, 35, 60),
    ("B0CNYV5678", "NYVOS-ASH-60",  "Ashwagandha KSM-66 600mg · 60 caps",    7.80, 28, 60),
    ("B0CNYV6789", "NYVOS-BCX-90",  "B-Complex High Potency · 90 caps",       4.30, 25, 60),
    ("B0CNYV7890", "NYVOS-ZNS-60",  "Zinc + Selenium · 60 caps",              3.90, 20, 45),
    ("B0CNYV8901", "NYVOS-PRO-30",  "Probiotic 10 Billion CFU · 30 caps",    11.20, 30, 60),
]
_DEMO_VEL = {
    (0,"ES"):4.2,(0,"FR"):3.1,(0,"DE"):6.8,(0,"IT"):2.4,
    (1,"ES"):5.5,(1,"FR"):4.8,(1,"DE"):9.1,(1,"IT"):3.2,
    (2,"ES"):2.1,(2,"FR"):1.9,(2,"DE"):3.4,(2,"IT"):1.1,
    (3,"ES"):1.4,(3,"FR"):1.2,(3,"DE"):2.0,(3,"IT"):0.8,
    (4,"ES"):3.3,(4,"FR"):2.7,(4,"DE"):5.1,(4,"IT"):1.9,
    (5,"ES"):6.0,(5,"FR"):5.2,(5,"DE"):8.4,(5,"IT"):3.8,
    (6,"ES"):2.8,(6,"FR"):2.1,(6,"DE"):4.0,(6,"IT"):1.5,
    (7,"ES"):1.9,(7,"FR"):1.6,(7,"DE"):3.1,(7,"IT"):1.0,
}
_INV_MULT = [0.5, 0.8, 1.4, 2.2, 1.0, 0.3, 1.6, 2.5]


def _sparkline_data(avg_vel: float, seed: int) -> list[float]:
    random.seed(seed)
    data = []
    for _ in range(30):
        data.append(max(0.0, round(avg_vel + random.gauss(0, avg_vel * 0.3), 1)))
    return data


def _demo_master(warn_buffer: int = 15) -> list[dict]:
    rows = []
    for i, (asin, sku, name, cogs, lead, target) in enumerate(_DEMO_PRODUCTS):
        countries, total_avail, total_inbound, total_vel, total_value, worst = [], 0, 0, 0.0, 0.0, "ok"
        for mp in MARKETPLACES:
            vel   = _DEMO_VEL.get((i, mp), 2.0)
            avail = max(0, int(vel * lead * _INV_MULT[i]))
            inb   = int(vel * 15) if _INV_MULT[i] < 1.0 else 0
            val   = round(avail * cogs, 2)
            days  = round(avail / vel, 1) if vel > 0 else 9999.0
            st_   = "critical" if days < lead else ("warning" if days < lead + warn_buffer else "ok")
            if st_ == "critical" or (st_ == "warning" and worst == "ok"):
                worst = st_
            countries.append({"mp": mp, "flag": FLAGS[mp], "avail": avail, "inbound": inb,
                               "vel": round(vel, 1), "days_left": days, "value": val, "status": st_,
                               "reorder_qty": max(0, int((target - days) * vel)) if days < target else 0})
            total_avail += avail; total_inbound += inb; total_vel += vel; total_value += val

        avg_vel_total = round(total_vel / 4, 1)
        total_days = round(total_avail / avg_vel_total, 1) if avg_vel_total > 0 else 9999.0

        random.seed(i * 7)
        tf = random.uniform(0.7, 1.3)
        trend = "up" if tf > 1.08 else ("down" if tf < 0.92 else "flat")

        rows.append({"asin": asin, "sku": sku, "name": name, "cogs": cogs, "lead": lead, "target": target,
                     "total_avail": total_avail, "total_inbound": total_inbound,
                     "avg_vel": avg_vel_total, "total_days": total_days,
                     "total_value": round(total_value, 2), "worst": worst, "trend": trend,
                     "sparkline": _sparkline_data(avg_vel_total, i), "countries": countries})
    return rows


def _demo_sync_log() -> list[dict]:
    now, log = datetime.now(timezone.utc), []
    for h in [0, 6, 12, 18]:
        for stype in ["inventory", "sales"]:
            for mp in MARKETPLACES:
                log.append({"sync_type": stype, "marketplace": mp, "status": "success",
                             "records_updated": random.randint(6, 24),
                             "synced_at": (now - timedelta(hours=h)).strftime("%Y-%m-%d %H:%M UTC")})
    return log[:16]


# ══════════════════════════════════════════════════════════════
# DATA LOADERS
# ══════════════════════════════════════════════════════════════

@st.cache_data(ttl=300)
def load_master(warn_buffer: int = 15) -> list[dict]:
    if DEMO_MODE:
        return _demo_master(warn_buffer)

    # Load products
    products_raw = db.table("products").select(
        "asin,product_name,sku,cogs_eur,lead_time_days,target_days_coverage"
    ).execute().data or []
    if not products_raw:
        return []

    # Load active POs (ordered + shipped = on the way from supplier)
    po_raw = []
    try:
        from supabase import create_client as _cc
        adm = _cc(_SB_URL, os.environ.get("SUPABASE_SERVICE_ROLE_KEY",""))
        po_raw = adm.table("purchase_orders").select(
            "asin,units_ordered,status,est_arrival"
        ).in_("status", ["ordered","shipped"]).execute().data or []
    except Exception:
        pass
    # Sum on-order units per ASIN
    po_on_order: dict[str, int] = {}
    for po in po_raw:
        a = po.get("asin") or ""
        if a:
            po_on_order[a] = po_on_order.get(a, 0) + int(po.get("units_ordered") or 0)

    if not products_raw:
        return []

    # Load latest inventory snapshot per (asin, marketplace)
    inv_raw = db.table("inventory_snapshots").select(
        "asin,marketplace,units_available,units_inbound,stock_value_eur,snapshot_date"
    ).order("snapshot_date", desc=True).execute().data or []

    # Load reorder alerts
    alerts_raw = db.table("reorder_alerts").select(
        "asin,marketplace,days_of_stock_left,days_until_reorder,alert_status,suggested_reorder_qty"
    ).execute().data or []

    # Load latest sales velocity
    vel_raw = db.table("sales_velocity").select(
        "asin,marketplace,velocity_daily"
    ).execute().data or []
    vel_idx: dict[tuple, float] = {
        (r["asin"], r["marketplace"]): float(r.get("velocity_daily") or 0)
        for r in vel_raw
    }

    # Index by (asin, marketplace) — keep most recent inventory
    inv_idx: dict[tuple, dict] = {}
    for r in inv_raw:
        key = (r["asin"], r["marketplace"])
        if key not in inv_idx:
            inv_idx[key] = r

    alert_idx: dict[tuple, dict] = {
        (r["asin"], r["marketplace"]): r for r in alerts_raw
    }

    rows = []
    for p in products_raw:
        asin   = p["asin"]
        cogs   = p.get("cogs_eur") or 0.0
        lead   = p.get("lead_time_days") or 30
        target = p.get("target_days_coverage") or 60

        countries = []
        total_avail = total_inbound = 0
        total_vel   = 0.0
        total_value = 0.0
        worst       = "ok"

        for mp in MARKETPLACES:
            key   = (asin, mp)
            inv   = inv_idx.get(key, {})
            alert = alert_idx.get(key, {})

            avail   = int(inv.get("units_available") or 0)
            inbound = int(inv.get("units_inbound") or 0)
            vel     = vel_idx.get(key, 0.0)
            days    = float(alert.get("days_of_stock_left") or (avail / vel if vel > 0 else 9999.0))
            status  = alert.get("alert_status") or (
                "critical" if days < lead else ("warning" if days < lead + warn_buffer else "ok")
            )
            value   = round(avail * cogs, 2) if cogs else float(inv.get("stock_value_eur") or 0)
            reorder = int(alert.get("suggested_reorder_qty") or 0)

            if status == "critical" or (status == "warning" and worst == "ok"):
                worst = status

            units_30d_mp = round(vel * 30)
            countries.append({"mp": mp, "flag": FLAGS[mp], "avail": avail, "inbound": 0,
                               "vel": round(vel, 1), "days_left": round(days, 1), "value": value,
                               "status": status, "reorder_qty": reorder,
                               "units_30d": int(units_30d_mp)})

            total_avail   += avail
            total_inbound  = max(total_inbound, inbound)  # EU total only at product level
            total_vel     += vel
            total_value   += value

        avg_vel    = round(total_vel / 4, 1)
        total_days = round(total_avail / avg_vel, 1) if avg_vel > 0 else 9999.0
        trend      = "flat"
        on_order   = po_on_order.get(asin, 0)   # units ordered from supplier, not yet at FBA

        rows.append({"asin": asin, "sku": p.get("sku") or "", "name": p.get("product_name") or asin,
                     "cogs": cogs, "lead": lead, "target": target,
                     "total_avail": total_avail, "total_inbound": total_inbound,
                     "on_order": on_order,
                     "avg_vel": avg_vel, "total_days": total_days,
                     "total_value": round(total_value, 2), "worst": worst, "trend": trend,
                     "sparkline": [], "countries": countries})
    return rows


@st.cache_data(ttl=300)
def load_sync_log() -> list[dict]:
    if DEMO_MODE:
        return _demo_sync_log()
    rows = db.table("sync_log").select("*").order("synced_at", desc=True).limit(20).execute().data or []
    return rows


def clear_caches():
    load_master.clear()
    load_sync_log.clear()


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def make_sparkline(values: list[float], w: int = 60, h: int = 22) -> str:
    vals = [v for v in (values or []) if v is not None]
    if len(vals) < 2:
        return f'<svg width="{w}" height="{h}"></svg>'
    mn, mx = min(vals), max(vals)
    rng = mx - mn or 1
    pts = []
    for idx, v in enumerate(vals):
        x = 1 + idx * (w - 2) / (len(vals) - 1)
        y = h - 2 - (v - mn) / rng * (h - 4)
        pts.append(f"{x:.1f},{y:.1f}")
    return (f'<svg width="{w}" height="{h}" style="display:block;overflow:visible">'
            f'<path d="M {" L ".join(pts)}" fill="none" stroke="#C8FF00" '
            f'stroke-width="1.5" stroke-linejoin="round"/></svg>')


def trend_html(t: str) -> str:
    return {"up":   '<span style="color:#3B6D11;font-weight:800;font-size:15px">↑</span>',
            "down": '<span style="color:#E24B4A;font-weight:800;font-size:15px">↓</span>',
            "flat": '<span style="color:#999;font-size:15px">→</span>'}.get(t, "")


def days_style(d) -> str:
    try:
        v = float(d)
    except (TypeError, ValueError):
        return "color:#999"
    red_thr   = st.session_state.get("days_red",   30)
    amber_thr = st.session_state.get("days_amber", 60)
    if v < red_thr:   return "color:#E24B4A;font-weight:700"
    if v < amber_thr: return "color:#BA7517;font-weight:700"
    return "color:#639922;font-weight:700"


def fmt_days(d) -> str:
    try:
        v = float(d)
        return "∞" if v >= 9999 else f"{v:.0f}d"
    except Exception:
        return "—"


def status_dot(s: str) -> str:
    c = {"critical": "#E24B4A", "warning": "#F5A623", "ok": "#4CAF50"}.get(s, "#999")
    return f'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:{c}"></span>'


def mp_status_dots(countries: list[dict]) -> str:
    """4 mini dots in ES/FR/DE/IT order — one per marketplace with tooltip."""
    mp_status = {c["mp"]: c["status"] for c in countries}
    COLOR = {"critical": "#E24B4A", "warning": "#F5A623", "ok": "#4CAF50"}
    parts = []
    for mp in MARKETPLACES:
        st  = mp_status.get(mp, "ok")
        col = COLOR.get(st, "#999")
        parts.append(
            f'<span title="{FLAGS[mp]} {mp}: {st}" '
            f'style="display:inline-block;width:8px;height:8px;border-radius:50%;'
            f'background:{col};margin:0 2px;cursor:default"></span>'
        )
    return "".join(parts)


# ══════════════════════════════════════════════════════════════
# TABLE HTML
# ══════════════════════════════════════════════════════════════

def build_table_html(rows: list[dict], active_mps: list[str], active_alerts: list[str]) -> str:
    filtered = [r for r in rows if not active_alerts or r["worst"] in active_alerts]

    COLS = [
        ("Product",         "220px", False),
        ("ASIN",            "108px", False),
        ("Units",           "72px",  True),
        ("On order",        "72px",  True),
        ("Vel/day",         "68px",  True),
        ("",                "36px",  True),   # trend
        ("30d sales",       "72px",  True),
        ("Days left",       "80px",  True),
        ("Stock €",         "82px",  True),
        ("Alert",           "62px",  True),
        ("",                "30px",  True),   # expand
    ]

    SORTABLE = {"Units": "units", "30d sales": "sales", "Days left": "days", "Stock €": "stock"}

    ths = ""
    for label, width, right in COLS:
        align = "right" if right else "left"
        color = "#C8FF00"
        sort_attr = SORTABLE.get(label, "")
        cursor = "cursor:pointer;user-select:none;" if sort_attr else ""
        onclick = f' onclick="sortTable(\'{sort_attr}\')"' if sort_attr else ""
        sort_indicator = f' <span id="si-{sort_attr}" style="font-size:9px;opacity:0.5"></span>' if sort_attr else ""
        ths += (f'<th{onclick} style="width:{width};min-width:{width};text-align:{align};'
                f'color:{color};padding:10px 8px;font-size:10.5px;font-weight:600;'
                f'text-transform:uppercase;letter-spacing:0.06em;white-space:nowrap;{cursor}">'
                f'{label}{sort_indicator}</th>')

    body = ""
    for ri, r in enumerate(filtered):
        pid  = f"p{ri}"
        ds         = days_style(r["total_days"])
        units_30d  = sum(c["vel"] * 30 for c in r["countries"])

        def td(content, style="", right=False):
            a = "right" if right else "left"
            return (f'<td style="padding:9px 8px;vertical-align:middle;'
                    f'text-align:{a};white-space:nowrap;overflow:hidden;'
                    f'text-overflow:ellipsis;{style}">{content}</td>')

        cells = (
            td(f'<div style="font-weight:600;font-size:13px;color:#111;white-space:nowrap;'
               f'overflow:hidden;text-overflow:ellipsis;max-width:200px">{r["name"]}</div>'
               f'<div style="font-size:11px;color:#888;margin-top:1px">{r["sku"]}</div>')
            + td(f'<span style="background:#F4F4F2;border:1px solid #E0E0E0;border-radius:4px;'
                 f'padding:2px 5px;font-size:11px;font-family:monospace;color:#555">'
                 f'{r["asin"]}</span>')
            + td(f'{r["total_avail"]:,}', right=True)
            + td(
                f'<span style="color:#1A56DB;font-weight:{"700" if r.get("on_order",0)>0 else "400"}">'
                f'{r.get("on_order", 0):,}</span>',
                right=True
              )
            + td(f'{r["avg_vel"]:.1f}', right=True)
            + td(trend_html(r["trend"]), style="text-align:center")
            + td(f'{int(units_30d):,}', right=True)
            + td(fmt_days(r["total_days"]), style=ds, right=True)
            + td(f'€{r["total_value"]:,.0f}', right=True)
            + td(mp_status_dots(r["countries"]), style="text-align:center")
            + td(f'<span id="btn-{pid}" style="display:inline-flex;align-items:center;'
                 f'justify-content:center;width:20px;height:20px;border-radius:4px;'
                 f'background:#F0F0F0;font-size:9px;color:#555;cursor:pointer;'
                 f'user-select:none">▶</span>', style="text-align:center;cursor:pointer")
        )
        days_val = r["total_days"] if r["total_days"] < 9999 else 999999
        body += (f'<tr class="mr" data-pid="{pid}" '
                 f'data-units="{r["total_avail"]}" data-sales="{int(units_30d)}" data-days="{days_val}" data-stock="{r["total_value"]}" '
                 f'style="cursor:pointer;border-bottom:1px solid #F0F0F0;background:{"#FFFFFF" if ri%2==0 else "#FAFAFA"}">'
                 f'{cells}</tr>\n')

        for c in r["countries"]:
            if active_mps and c["mp"] not in active_mps:
                continue
            dsc = days_style(c["days_left"])
            sub_cells = (
                td(f'<span style="font-size:12px;font-weight:600;color:#444">'
                   f'{c["flag"]} {c["mp"]}</span>', style="padding-left:26px")
                + td("")
                + td(f'{c["avail"]:,}', right=True)
                + td("", right=True)   # On order is EU-level only, blank per country
                + td(f'{c["vel"]:.1f}', right=True)
                + td("")
                + td(f'{c.get("units_30d", 0):,}', right=True)
                + td(fmt_days(c["days_left"]), style=dsc, right=True)
                + td(f'€{c["value"]:,.0f}', right=True)
                + td(status_dot(c["status"]), style="text-align:center")
                + td("")
            )
            body += (f'<tr id="sub-{pid}-{c["mp"]}" style="display:none;'
                     f'background:#F7F7F5;border-bottom:1px solid #EEEEEE;'
                     f'font-size:12px">{sub_cells}</tr>\n')

    if not filtered:
        body = ('<tr><td colspan="10" style="text-align:center;padding:48px;'
                'color:#888;font-size:14px">No products match the current filters.</td></tr>')

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Inter',sans-serif;font-size:13px;
       background:transparent;color:#111; }}
.tw {{ overflow-x:auto;border-radius:10px;border:1px solid #E8E8E8;
      background:white;box-shadow:0 1px 4px rgba(0,0,0,.06); }}
table {{ width:100%;border-collapse:collapse;table-layout:fixed; }}
thead tr {{ background:#111111; }}
.mr:hover {{ background:#F5F5F0 !important; }}
tr[id^="sub-"]:hover {{ background:#F0F0EE !important; }}
td {{ max-width:0;overflow:hidden;text-overflow:ellipsis; }}
.ctrl-bar {{ display:flex;align-items:center;gap:8px;padding:8px 10px 6px; }}
.ctrl-btn {{ background:#F0F0F0;border:1px solid #DDD;border-radius:5px;
             padding:4px 10px;font-size:11px;cursor:pointer;color:#444;
             font-family:inherit; }}
.ctrl-btn:hover {{ background:#E8E8E8; }}
</style></head><body>
<div class="ctrl-bar">
  <button class="ctrl-btn" onclick="expandAll()">⊞ Expand all</button>
  <button class="ctrl-btn" onclick="collapseAll()">⊟ Collapse all</button>
</div>
<div class="tw"><table>
<thead><tr>{ths}</tr></thead>
<tbody id="tbody">{body}</tbody>
</table></div>
<script>
var sortState = {{}};

function resizeFrame() {{
  setTimeout(function() {{
    window.parent.postMessage({{isStreamlitMessage:true,
      type:'streamlit:setFrameHeight',height:document.body.scrollHeight+10}},'*');
  }}, 80);
}}

function toggleRow(pid) {{
  var btn = document.getElementById('btn-' + pid);
  var open = btn.textContent === '▼';
  document.querySelectorAll('[id^="sub-' + pid + '-"]').forEach(function(r) {{
    r.style.display = open ? 'none' : 'table-row';
  }});
  btn.textContent = open ? '▶' : '▼';
  resizeFrame();
}}

function expandAll() {{
  document.querySelectorAll('.mr').forEach(function(row) {{
    var pid = row.dataset.pid;
    document.querySelectorAll('[id^="sub-' + pid + '-"]').forEach(function(r) {{
      r.style.display = 'table-row';
    }});
    var btn = document.getElementById('btn-' + pid);
    if (btn) btn.textContent = '▼';
  }});
  resizeFrame();
}}

function collapseAll() {{
  document.querySelectorAll('.mr').forEach(function(row) {{
    var pid = row.dataset.pid;
    document.querySelectorAll('[id^="sub-' + pid + '-"]').forEach(function(r) {{
      r.style.display = 'none';
    }});
    var btn = document.getElementById('btn-' + pid);
    if (btn) btn.textContent = '▶';
  }});
  resizeFrame();
}}

function sortTable(col) {{
  var tbody = document.getElementById('tbody');
  var rows = Array.from(document.querySelectorAll('.mr'));
  var asc = sortState[col] !== 'asc';
  sortState = {{}};
  sortState[col] = asc ? 'asc' : 'desc';
  ['units','sales','days','stock'].forEach(function(c) {{
    var el = document.getElementById('si-' + c);
    if (el) el.textContent = '';
  }});
  var si = document.getElementById('si-' + col);
  if (si) si.textContent = asc ? ' ↑' : ' ↓';

  rows.sort(function(a, b) {{
    var av = parseFloat(a.dataset[col]) || 0;
    var bv = parseFloat(b.dataset[col]) || 0;
    return asc ? av - bv : bv - av;
  }});

  rows.forEach(function(row) {{
    var pid = row.dataset.pid;
    tbody.appendChild(row);
    document.querySelectorAll('[id^="sub-' + pid + '-"]').forEach(function(sub) {{
      tbody.appendChild(sub);
    }});
  }});
  resizeFrame();
}}

document.querySelectorAll('.mr').forEach(function(row) {{
  row.addEventListener('click', function() {{
    toggleRow(this.dataset.pid);
  }});
}});

window.addEventListener('load', resizeFrame);
</script></body></html>"""


# ══════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════

def render_sidebar():
    with st.sidebar:
        # ── Logo ──────────────────────────────────────────────
        st.markdown(f"""
<div style="padding:24px 16px 20px;border-bottom:1px solid #222;margin-bottom:8px">
  {LOGO_IMG_SM}
  <div style="font-size:11px;color:#666;margin-top:8px;letter-spacing:.1em;font-weight:600">AMAZON EU DASHBOARD</div>
</div>""", unsafe_allow_html=True)

        # ── Navigation buttons ─────────────────────────────────
        current = st.session_state.get("page", "inventory")
        nav_items = [("📦", "Inventory",       "inventory"),
                     ("🚚", "Reorder",        "reorder"),
                     ("📋", "Orders",         "orders"),
                     ("📈", "Forecast",       "forecast"),
                     ("⚙️", "Settings",       "settings")]

        for icon, label, key in nav_items:
            active = current == key
            if st.button(f"{icon}  {label}", key=f"nav_{key}",
                         use_container_width=True,
                         type="primary" if active else "secondary"):
                st.session_state.page = key
                st.rerun()

        mode_text = "🧪 Demo mode" if DEMO_MODE else "🟢 Live"
        st.markdown(f"""
<div style="position:fixed;bottom:24px;font-size:11px;color:#444;text-align:center;width:200px">
  {mode_text}
</div>""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
# MARKETPLACE CARDS
# ══════════════════════════════════════════════════════════════

def render_mp_cards(rows: list[dict]):
    stats = {mp: {"avail":0,"value":0.0,"sales30d":0,"critical":0,"warning":0,"ok":0} for mp in MARKETPLACES}
    for r in rows:
        for c in r["countries"]:
            s = stats[c["mp"]]
            s["avail"] += c["avail"]
            s["value"] += c["value"]
            s["sales30d"] += c.get("units_30d", 0)
            s[c["status"]] += 1

    html = '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px">'
    for mp in MARKETPLACES:
        s = stats[mp]
        html += f"""
<div class="mp-card">
  <div style="font-size:22px;margin-bottom:3px">{FLAGS[mp]}</div>
  <div style="font-size:12px;color:#888;font-weight:600;letter-spacing:.04em;margin-bottom:8px;text-transform:uppercase">{COUNTRY_NAMES[mp]}</div>
  <div style="display:flex;align-items:baseline;gap:6px;margin-bottom:4px">
    <span style="font-size:32px;font-weight:800;color:#111;line-height:1">{s['avail']:,}</span>
    <span style="font-size:12px;color:#aaa;font-weight:500">units</span>
  </div>
  <div style="font-size:13px;color:#555;font-weight:600;margin-bottom:3px">€{s['value']:,.0f} stock value</div>
  <div style="font-size:12px;color:#888;margin-bottom:12px">{s['sales30d']:,} units sold 30d</div>
  <div style="display:flex;gap:5px;flex-wrap:wrap">
    <span style="padding:3px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#FCEBEB;color:#A32D2D">{s['critical']} critical</span>
    <span style="padding:3px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#FAEEDA;color:#854F0B">{s['warning']} warning</span>
    <span style="padding:3px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#EAF3DE;color:#3B6D11">{s['ok']} ok</span>
  </div>
</div>"""
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
# EXCEL EXPORT
# ══════════════════════════════════════════════════════════════

def export_excel(rows: list[dict]) -> bytes:
    out = []
    for r in rows:
        out.append({"Type":"TOTAL","Product":r["name"],"ASIN":r["asin"],"SKU":r["sku"],
                    "COGS_EUR":r["cogs"],"Lead_days":r["lead"],"Marketplace":"ALL",
                    "Units":r["total_avail"],"Inbound":r["total_inbound"],
                    "Vel_day":r["avg_vel"],"Days_left":r["total_days"],
                    "Stock_EUR":r["total_value"],"Alert":r["worst"]})
        for c in r["countries"]:
            out.append({"Type":"country","Product":r["name"],"ASIN":r["asin"],"SKU":r["sku"],
                        "COGS_EUR":r["cogs"],"Lead_days":r["lead"],"Marketplace":c["mp"],
                        "Units":c["avail"],"Inbound":c["inbound"],"Vel_day":c["vel"],
                        "Days_left":c["days_left"],"Stock_EUR":c["value"],"Alert":c["status"]})
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        pd.DataFrame(out).to_excel(w, index=False, sheet_name="Inventory")
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════
# SYNC DIALOG
# ══════════════════════════════════════════════════════════════

@st.dialog("🕐 Sync Status", width="large")
def sync_dialog():
    if DEMO_MODE:
        st.info("Demo mode — showing simulated history.", icon="🧪")

    log = load_sync_log()
    now = datetime.now(timezone.utc)

    st.markdown("**Last successful sync per marketplace:**")
    cols = st.columns(4)
    for i, mp in enumerate(MARKETPLACES):
        with cols[i]:
            mp_entries = [l for l in log if l.get("marketplace") == mp and l.get("status") == "success"]
            if mp_entries:
                try:
                    t = datetime.strptime(mp_entries[0]["synced_at"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                    age = int((now - t).total_seconds() // 3600)
                    label = f"{age}h ago"
                except Exception:
                    label = "recent"
            else:
                label = "Never"
            st.metric(f"{FLAGS[mp]} {mp}", label)

    st.divider()
    st.markdown("**Recent log:**")
    if log:
        df = pd.DataFrame(log[:10])
        df["status"] = df["status"].map({"success": "✅ ok", "error": "❌ error"}).fillna(df["status"])
        st.dataframe(df[["synced_at","sync_type","marketplace","status","records_updated"]],
                     hide_index=True, use_container_width=True)

    st.divider()
    if DEMO_MODE:
        st.button("🔄 Sync Now", disabled=True, help="Connect SP-API + Supabase to enable")
    elif st.button("🔄 Sync Now", type="primary"):
        with st.spinner("Running full sync…"):
            try:
                from backend.fetchers.catalog import fetch_catalog
                from backend.fetchers.inventory import fetch_all_inventory
                from backend.fetchers.sales import fetch_all_sales
                from backend.reorder import calculate_reorder_alerts
                fetch_catalog(); fetch_all_inventory(); fetch_all_sales(); calculate_reorder_alerts()
                clear_caches(); st.success("Sync complete!"); st.rerun()
            except Exception as e:
                st.error(f"Sync failed: {e}")


# ══════════════════════════════════════════════════════════════
# PURCHASE ORDERS PAGE
# ══════════════════════════════════════════════════════════════

STATUS_COLORS = {
    "draft":     ("#F4F4F2", "#555"),
    "ordered":   ("#FAEEDA", "#854F0B"),
    "shipped":   ("#E8F0FE", "#1A56DB"),
    "received":  ("#EAF3DE", "#3B6D11"),
    "cancelled": ("#F4F4F2", "#999"),
}
STATUS_LABELS = {
    "draft": "Draft", "ordered": "Ordered",
    "shipped": "Shipped", "received": "Received", "cancelled": "Cancelled",
}


def _load_pos() -> list[dict]:
    if DEMO_MODE:
        return []
    try:
        from supabase import create_client as _cc
        svc_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not svc_key:
            st.warning("SUPABASE_SERVICE_ROLE_KEY not set in .env — cannot load orders.", icon="⚠️")
            return []
        adm = _cc(_SB_URL, svc_key)
        return adm.table("purchase_orders").select("*").order("po_date", desc=True).execute().data or []
    except Exception as e:
        st.error(f"Could not load purchase orders: {e}")
        return []


def _save_pos(rows_to_upsert: list[dict]) -> bool:
    try:
        from supabase import create_client as _cc
        adm = _cc(_SB_URL, os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""))
        adm.table("purchase_orders").upsert(rows_to_upsert).execute()
        return True
    except Exception as e:
        st.error(f"Save failed: {e}")
        return False


def render_orders_page(inventory_rows: list[dict]):
    from datetime import date as _date
    import io as _io

    st.markdown("#### 📋 Purchase Orders")

    if DEMO_MODE:
        st.info("Connect Supabase to manage purchase orders.", icon="🧪")
        return

    pos = _load_pos()

    # ── Summary cards ──────────────────────────────────────────
    stats = {"draft": [0, 0.0, 0], "ordered": [0, 0.0, 0],
             "shipped": [0, 0.0, 0], "received": [0, 0.0, 0]}
    for p in pos:
        s = p.get("status", "ordered")
        if s in stats:
            stats[s][0] += 1
            stats[s][1] += float(p.get("cost_eur") or 0)
            stats[s][2] += int(p.get("units_ordered") or 0)

    card_html = '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px">'
    for st_key, (bg, fg) in list(STATUS_COLORS.items())[:4]:
        cnt, cost, units = stats.get(st_key, [0, 0.0, 0])
        card_html += f"""
<div style="background:{bg};border-radius:10px;border:1px solid #E0E0E0;padding:16px 18px">
  <div style="font-size:11px;color:{fg};font-weight:700;text-transform:uppercase;
              letter-spacing:.06em;margin-bottom:8px">{STATUS_LABELS[st_key]}</div>
  <div style="font-size:13px;color:#888;margin-bottom:2px">PO count</div>
  <div style="font-size:26px;font-weight:800;color:#111;line-height:1">{cnt}</div>
  <div style="font-size:12px;color:#666;margin-top:4px">€{cost:,.2f} · {units:,} units</div>
</div>"""
    card_html += "</div>"
    st.markdown(card_html, unsafe_allow_html=True)

    # ── Shared helpers ─────────────────────────────────────────
    asin_name = {r["asin"]: r["name"] for r in inventory_rows}
    asin_opts = [r["asin"] for r in inventory_rows]
    asin_lbls = {r["asin"]: f"{r['asin']} · {r['name']}" for r in inventory_rows}

    # ── Tabs: View / Add / Import ──────────────────────────────
    t_view, t_add, t_import = st.tabs(["📋 All Orders", "➕ Add Order", "📥 Import from CSV"])

    # ══ Tab 1: View & edit ════════════════════════════════════
    with t_view:
        if not pos:
            st.info("No purchase orders yet. Use **➕ Add Order** or **📥 Import from CSV** to get started.")
        else:
            # Filters
            fc1, fc2 = st.columns(2)
            with fc1:
                status_filter = st.multiselect(
                    "Filter by status", list(STATUS_LABELS.keys()),
                    default=["draft","ordered","shipped"],
                    format_func=lambda x: STATUS_LABELS[x],
                    key="po_status_filter",
                )
            with fc2:
                asin_filter = st.text_input("Search ASIN or product", key="po_search",
                                            placeholder="🔍 Type to filter…")

            filtered = [p for p in pos
                        if (not status_filter or p.get("status") in status_filter)
                        and (not asin_filter or
                             asin_filter.lower() in (p.get("asin") or "").lower() or
                             asin_filter.lower() in (p.get("product_name") or "").lower())]

            st.caption(f"{len(filtered)} of {len(pos)} orders")

            def _to_date(val):
                if not val:
                    return None
                try:
                    return pd.to_datetime(str(val)).date()
                except Exception:
                    return None

            rows_disp = []
            for p in filtered:
                rows_disp.append({
                    "id":           p["id"],
                    "PO #":         p.get("po_number") or "",
                    "Date":         _to_date(p.get("po_date")),
                    "ASIN":         p.get("asin") or "",
                    "Product":      p.get("product_name") or asin_name.get(p.get("asin",""),""),
                    "Supplier":     p.get("supplier") or "",
                    "Units":        int(p.get("units_ordered") or 0),
                    "Cost (€)":     float(p.get("cost_eur") or 0),
                    "Est. arrival": _to_date(p.get("est_arrival")),
                    "Status":       p.get("status") or "ordered",
                    "Notes":        p.get("notes") or "",
                })

            df_disp = pd.DataFrame(rows_disp)
            edited  = st.data_editor(
                df_disp.drop(columns=["id"]),
                column_config={
                    "PO #":         st.column_config.TextColumn("PO #"),
                    "Date":         st.column_config.DateColumn("Date"),
                    "ASIN":         st.column_config.TextColumn("ASIN", disabled=True),
                    "Product":      st.column_config.TextColumn("Product", width="large"),
                    "Supplier":     st.column_config.TextColumn("Supplier"),
                    "Units":        st.column_config.NumberColumn("Units", min_value=0),
                    "Cost (€)":     st.column_config.NumberColumn("Cost (€)", format="€%.2f"),
                    "Est. arrival": st.column_config.DateColumn("Est. arrival"),
                    "Status":       st.column_config.SelectboxColumn(
                        "Status", options=list(STATUS_LABELS.keys()),
                        required=True),
                    "Notes":        st.column_config.TextColumn("Notes"),
                },
                hide_index=True,
                use_container_width=True,
                key="po_editor",
                num_rows="fixed",
            )

            if st.button("💾 Save changes", type="primary", key="save_po_edits"):
                to_save = []
                for i, row in edited.iterrows():
                    to_save.append({
                        "id":           int(df_disp.iloc[i]["id"]),
                        "po_number":    row["PO #"],
                        "po_date":      str(row["Date"]) if row["Date"] else None,
                        "product_name": row["Product"],
                        "supplier":     row["Supplier"],
                        "units_ordered": int(row["Units"]),
                        "cost_eur":     float(row["Cost (€)"]),
                        "est_arrival":  str(row["Est. arrival"]) if row["Est. arrival"] else None,
                        "status":       row["Status"],
                        "notes":        row["Notes"],
                    })
                if _save_pos(to_save):
                    st.success("Saved!")
                    st.rerun()

    # ══ Tab 2: Add single order ════════════════════════════════
    with t_add:
        st.markdown("**Add a new purchase order**")
        a1, a2 = st.columns(2)
        with a1:
            new_asin = st.selectbox("ASIN", options=asin_opts,
                                    format_func=lambda x: asin_lbls.get(x, x),
                                    key="new_po_asin")
            new_po_num  = st.text_input("PO number", key="new_po_num")
            new_date    = st.date_input("PO date", value=_date.today(), key="new_po_date")
            new_supplier= st.text_input("Supplier", key="new_po_supplier",
                                        value="Laboratorios Bequisa S...")
        with a2:
            new_units   = st.number_input("Units ordered", min_value=1, step=10, key="new_po_units")
            new_cost    = st.number_input("Total cost (€)", min_value=0.0, step=0.01,
                                          format="%.2f", key="new_po_cost")
            new_arrival = st.date_input("Est. arrival", key="new_po_arrival")
            new_status  = st.selectbox("Status", options=list(STATUS_LABELS.keys()),
                                       format_func=lambda x: STATUS_LABELS[x],
                                       index=1, key="new_po_status")
        new_notes = st.text_input("Notes (optional)", key="new_po_notes")

        if st.button("➕ Add Order", type="primary", key="add_po_btn"):
            rec = {
                "po_number":    new_po_num or None,
                "po_date":      str(new_date),
                "asin":         new_asin,
                "product_name": asin_name.get(new_asin, ""),
                "supplier":     new_supplier or None,
                "units_ordered": int(new_units),
                "cost_eur":     float(new_cost),
                "est_arrival":  str(new_arrival),
                "status":       new_status,
                "notes":        new_notes or None,
            }
            if _save_pos([rec]):
                st.success(f"Order added! {int(new_units)} units arriving {new_arrival}")
                st.rerun()

    # ══ Tab 3: CSV import ═════════════════════════════════════
    with t_import:
        st.markdown("**Import from Sellerboard CSV export**")
        st.caption("Go to Sellerboard → Purchase Orders → Export CSV. "
                   "The file should have columns: "
                   "`PO Date`, `PO #`, `Supplier`, `Product`, `ASIN`, "
                   "`Total Units`, `Total Cost`, `Estimated Arrival`, `Status`")

        uploaded = st.file_uploader("Upload Sellerboard CSV / Excel",
                                    type=["csv","xlsx"], key="po_csv_upload")
        if uploaded:
            try:
                if uploaded.name.endswith(".xlsx"):
                    raw_df = pd.read_excel(uploaded)
                else:
                    raw_df = pd.read_csv(uploaded)

                # Normalise column names: strip whitespace + BOM
                raw_df.columns = [c.strip().lstrip("﻿") for c in raw_df.columns]

                st.markdown(f"**Preview** — {len(raw_df)} rows, columns: "
                            f"`{'`, `'.join(raw_df.columns)}`")
                st.dataframe(raw_df.head(5), use_container_width=True)

                def _parse_date(val) -> str | None:
                    """Handle DD/MM/YYYY, D/M/YY, YYYY-MM-DD, etc."""
                    if not val or str(val).strip() in ("", "nan", "None"):
                        return None
                    s = str(val).strip()
                    # Try day-first (European format)
                    for dayfirst in (True, False):
                        try:
                            return pd.to_datetime(s, dayfirst=dayfirst).strftime("%Y-%m-%d")
                        except Exception:
                            pass
                    return None

                def _parse_units(val) -> int:
                    s = str(val).strip().replace(",", "").replace(".", "")
                    try:
                        return int(float(s))
                    except Exception:
                        return 0

                def _parse_cost(val) -> float:
                    s = str(val).strip()
                    # Remove currency symbols and spaces
                    s = s.replace("€", "").replace("$", "").replace(" ", "")
                    # European format: dot=thousands, comma=decimal → normalise
                    if "," in s and "." in s:
                        s = s.replace(".", "").replace(",", ".")
                    elif "," in s:
                        s = s.replace(",", ".")
                    try:
                        return float(s)
                    except Exception:
                        return 0.0

                status_map = {
                    "ordered": "ordered", "order": "ordered",
                    "shipped": "shipped", "ship": "shipped",
                    "draft":   "draft",
                    "received":"received",
                    "cancelled":"cancelled", "canceled":"cancelled",
                }

                records = []
                known_asins = {r["asin"] for r in inventory_rows}

                for _, row in raw_df.iterrows():
                    asin     = str(row.get("ASIN", "") or "").strip()
                    pname    = str(row.get("Product", row.get("Products", "")) or "").strip()
                    status   = status_map.get(
                        str(row.get("Status","")).strip().lower(), "ordered")

                    # Warn but still import if ASIN not in products table
                    records.append({
                        "po_number":     str(row.get("PO #", row.get("PO number","")) or "").strip() or None,
                        "po_date":       _parse_date(row.get("PO Date", row.get("PO date"))),
                        "asin":          asin if asin in known_asins else None,
                        "product_name":  pname,
                        "supplier":      str(row.get("Supplier","") or "").strip() or None,
                        "units_ordered": _parse_units(row.get("Total Units", row.get("Total units", 0))),
                        "cost_eur":      _parse_cost(row.get("Total Cost",  row.get("Total cost",  0))),
                        "est_arrival":   _parse_date(row.get("Estimated Arrival", row.get("Estimated arrival"))),
                        "status":        status,
                    })

                unknown = [r["product_name"] for r in records if not r["asin"]]
                if unknown:
                    st.warning(
                        f"⚠️ {len(unknown)} rows have ASINs not in your product catalog "
                        f"(they'll be imported without ASIN link): "
                        f"{', '.join(set(unknown))[:300]}"
                    )

                st.success(f"✅ Ready to import **{len(records)} orders**.")

                if st.button(f"📥 Import {len(records)} orders", type="primary",
                             key="confirm_import"):
                    if _save_pos(records):
                        st.success(f"✅ Imported {len(records)} orders!")
                        st.rerun()

            except Exception as e:
                st.error(f"Error parsing file: {e}")


# ══════════════════════════════════════════════════════════════
# REORDER PLANNER  (forecast-driven when available)
# ══════════════════════════════════════════════════════════════

def render_reorder_planner(rows: list[dict]):
    import math as _math
    from datetime import date as _date, timedelta as _td

    today = _date.today()

    # ── Helper: YYYY-MM for offset months from today ───────────
    def _mk(offset: int) -> str:
        y = today.year + (today.month - 1 + offset) // 12
        m = (today.month - 1 + offset) % 12 + 1
        return f"{y:04d}-{m:02d}"

    def _fmt_ym(ym: str) -> str:
        try:
            return datetime.strptime(ym, "%Y-%m").strftime("%b %Y")
        except Exception:
            return ym

    # ── Load forecast (silent — no error shown here) ───────────
    forecast_data: dict = {}
    try:
        raw = load_forecast()
        if "_error" not in raw:
            forecast_data = raw
    except Exception:
        pass

    n_fc  = sum(1 for r in rows if r["asin"] in forecast_data)
    n_vel = len(rows) - n_fc

    # ── Source badge pill ──────────────────────────────────────
    if forecast_data:
        st.markdown(
            f'<div style="margin-bottom:8px;font-size:12px">'
            f'<span style="background:#EAF3DE;color:#3B6D11;font-weight:600;'
            f'padding:3px 8px;border-radius:12px">📈 {n_fc} forecast-driven</span>&nbsp;'
            f'<span style="background:#F4F4F2;color:#666;font-weight:600;'
            f'padding:3px 8px;border-radius:12px">📊 {n_vel} velocity fallback</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ── Build needs table ──────────────────────────────────────
    needs = []

    for r in rows:
        asin   = r["asin"]
        lead   = r.get("lead",   30)
        target = r.get("target", 60)
        fcast  = forecast_data.get(asin, {}).get("data")   # DataFrame or None

        for c in r["countries"]:
            mp      = c["mp"]
            flag    = c["flag"]
            stock   = c["avail"]
            inbound = c["inbound"]
            vel      = c["vel"]
            on_order = r.get("on_order", 0)   # units ordered from supplier (EU total)

            if fcast is not None:
                # ── Skip country if truly no demand (0 stock, 0 vel, 0 forecast) ──
                total_fc_country = sum(
                    int(fcast.loc[_mk(i), mp]) if _mk(i) in fcast.index else 0
                    for i in range(18)
                )
                if stock == 0 and vel == 0 and total_fc_country == 0:
                    continue   # nothing to sell here, skip

                # ── Forecast path ──────────────────────────
                # 1. Simulate stock depletion (include on_order as future supply)
                remaining    = stock + inbound + on_order
                stockout_ym  = None
                for offset in range(20):
                    mk = _mk(offset)
                    if mk not in fcast.index:
                        break
                    remaining -= int(fcast.loc[mk, mp])
                    if remaining < 0:   # strictly negative — avoids false trigger on 0-stock/0-fc
                        stockout_ym = mk
                        break

                if stockout_ym:
                    stockout_dt   = datetime.strptime(stockout_ym, "%Y-%m").date()
                    order_by_dt   = stockout_dt - _td(days=lead)
                    days_to_order = (order_by_dt - today).days
                    stockout_lbl  = _fmt_ym(stockout_ym)
                    order_by_lbl  = order_by_dt.strftime("%d %b %Y")
                else:
                    days_to_order = 9999
                    stockout_lbl  = "OK through forecast"
                    order_by_lbl  = "—"

                # 2. Reorder qty = forecast window − on-hand − already on order
                months_ahead = max(2, _math.ceil((lead + target) / 30))
                fc_window    = sum(
                    int(fcast.loc[_mk(i), mp]) if _mk(i) in fcast.index else 0
                    for i in range(months_ahead)
                )
                reorder_qty  = max(0, fc_window - stock - inbound - on_order)
                days_left    = round(stock / vel, 1) if vel > 0 else 9999
                source       = "📈 Forecast"

            else:
                # ── Velocity fallback ──────────────────────
                if stock == 0 and vel == 0:
                    continue   # no demand in this country, skip
                days_left     = round((stock + on_order) / vel, 1) if vel > 0 else 9999
                days_to_order = round(days_left - lead, 0)
                reorder_qty   = max(0, int((target - days_left) * vel)) if vel > 0 else 0
                stockout_lbl  = fmt_days(days_left)
                order_by_lbl  = (today + _td(days=max(0, int(days_to_order)))).strftime("%d %b %Y") \
                                 if days_to_order < 9999 else "—"
                source        = "📊 Velocity"

            # Status
            if days_to_order <= 0:
                alert = "🔴 Order now"
            elif days_to_order <= 14:
                alert = "🟡 Order soon"
            elif days_to_order <= 45:
                alert = "🟠 Upcoming"
            else:
                alert = "🟢 OK"

            # Only show rows that need attention (order within 45 days or already late)
            if days_to_order <= 45:
                needs.append({
                    "Product":       r["name"],
                    "MP":            f"{flag} {mp}",
                    "Source":        source,
                    "Stock":         stock,
                    "Inbound":       inbound,
                    "On order 📦":   on_order,
                    "Vel/day":       round(vel, 1),
                    "Stockout est.": stockout_lbl,
                    "Order by":      order_by_lbl,
                    "Days to order": int(days_to_order) if days_to_order < 9999 else 999,
                    "Alert":         alert,
                    "Reorder units": reorder_qty,
                    # hidden for Excel / shipment plan
                    "_asin":  asin,
                    "_lead":  lead,
                    "_mp_raw": mp,
                })

    if not needs:
        st.success("✅ All products well-stocked — no orders needed in the next 45 days.")
        return

    # Sort by urgency
    needs.sort(key=lambda x: x["Days to order"])

    # ── Summary counters ───────────────────────────────────────
    n_now  = sum(1 for n in needs if n["Days to order"] <= 0)
    n_soon = sum(1 for n in needs if 0 < n["Days to order"] <= 14)
    n_up   = sum(1 for n in needs if 14 < n["Days to order"] <= 45)
    st.markdown(
        f'<div style="display:flex;gap:10px;margin-bottom:12px">'
        f'<span style="background:#FCEBEB;color:#A32D2D;font-weight:700;padding:4px 12px;border-radius:20px">🔴 {n_now} order now</span>'
        f'<span style="background:#FAEEDA;color:#854F0B;font-weight:700;padding:4px 12px;border-radius:20px">🟡 {n_soon} order soon (&lt;14d)</span>'
        f'<span style="background:#FFF4E5;color:#7A4F00;font-weight:700;padding:4px 12px;border-radius:20px">🟠 {n_up} upcoming (&lt;45d)</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Editable table ─────────────────────────────────────────
    display_cols = ["Product","MP","Source","Stock","Inbound","On order 📦","Vel/day",
                    "Stockout est.","Order by","Days to order","Alert","Reorder units"]
    df_show = pd.DataFrame(needs)[display_cols]

    edited = st.data_editor(
        df_show,
        column_config={
            "Product":       st.column_config.TextColumn("Product",       width="large", disabled=True),
            "MP":            st.column_config.TextColumn("Marketplace",   disabled=True),
            "Source":        st.column_config.TextColumn("Source",        disabled=True),
            "Stock":         st.column_config.NumberColumn("Stock",        disabled=True),
            "Inbound":       st.column_config.NumberColumn("Inbound",      disabled=True),
            "On order 📦":   st.column_config.NumberColumn("On order 📦",  disabled=True,
                             help="Units ordered from supplier (ordered+shipped POs)"),
            "Vel/day":       st.column_config.NumberColumn("Vel/day",      format="%.1f", disabled=True),
            "Stockout est.": st.column_config.TextColumn("Stockout est.", disabled=True),
            "Order by":      st.column_config.TextColumn("Order by",      disabled=True),
            "Days to order": st.column_config.NumberColumn("Days to order", disabled=True),
            "Alert":         st.column_config.TextColumn("Alert",         disabled=True),
            "Reorder units": st.column_config.NumberColumn("✏️ Reorder units", min_value=0, step=10),
        },
        hide_index=True,
        use_container_width=True,
        key="reorder_editor",
    )

    # ── Shipment plan Excel ─────────────────────────────────────
    ship_rows = []
    for i, row in edited.iterrows():
        src     = needs[i]
        lead_d  = src["_lead"]
        ship_rows.append({
            "ASIN":           src["_asin"],
            "Product":        row["Product"],
            "Marketplace":    src["_mp_raw"],
            "Source":         row["Source"],
            "Stockout est.":  row["Stockout est."],
            "Order by":       row["Order by"],
            "Units to order": int(row["Reorder units"]),
            "Ship date":      str(today),
            "Est. arrival":   str(today + _td(days=lead_d)),
        })

    ship_df = pd.DataFrame(ship_rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        ship_df.to_excel(w, index=False, sheet_name="Shipment Plan")

    st.download_button(
        "📥 Generate Shipment Plan (Excel)",
        data=buf.getvalue(),
        file_name=f"nyvos_shipment_{today}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )


# ══════════════════════════════════════════════════════════════
# EDIT PANEL
# ══════════════════════════════════════════════════════════════

def render_edit_panel(rows: list[dict]):
    with st.expander("✏️ Edit Product Names, COGS & Lead Times", expanded=False):
        st.caption("Edit product names (shown everywhere in the dashboard), COGS, lead time and target coverage.")
        edit_df = pd.DataFrame([{"asin": r["asin"], "name": r["name"],
                                  "cogs_eur": r["cogs"], "lead_time_days": r["lead"],
                                  "target_coverage": r["target"]} for r in rows])
        orig = edit_df.copy()
        edited = st.data_editor(
            edit_df,
            column_config={
                "asin":            st.column_config.TextColumn("ASIN", disabled=True),
                "name":            st.column_config.TextColumn("Product name ✏️", width="large"),
                "cogs_eur":        st.column_config.NumberColumn("COGS (€)", format="€%.2f", min_value=0),
                "lead_time_days":  st.column_config.NumberColumn("Lead time (days)", min_value=1, max_value=365),
                "target_coverage": st.column_config.NumberColumn("Target coverage (days)", min_value=1, max_value=365),
            },
            hide_index=True, use_container_width=True, key="edit_products",
        )
        if st.button("💾 Save changes", type="primary", key="save_products"):
            if DEMO_MODE:
                st.info("Demo mode — changes aren't persisted. Connect Supabase to enable saving.", icon="🧪")
            else:
                changed = [(i, row) for i, (_, row) in enumerate(edited.iterrows())
                           if not row.equals(orig.iloc[i])]
                if changed:
                    from supabase import create_client as _cc
                    adm = _cc(_SB_URL, os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""))
                    for _, row in changed:
                        adm.table("products").update({
                            "product_name":        str(row["name"]),
                            "cogs_eur":            float(row["cogs_eur"]),
                            "lead_time_days":      int(row["lead_time_days"]),
                            "target_days_coverage":int(row["target_coverage"]),
                        }).eq("asin", row["asin"]).execute()
                    clear_caches()
                    st.success(f"✅ Saved {len(changed)} product(s).")
                    st.rerun()
                else:
                    st.info("No changes detected.")


# ══════════════════════════════════════════════════════════════
# FORECAST PAGE
# ══════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False)
def load_forecast() -> dict:
    try:
        from backend.fetchers.forecast import fetch_forecast
        return fetch_forecast()
    except Exception as e:
        return {"_error": str(e)}


def render_forecast_page(rows: list[dict]):
    import calendar
    from datetime import date

    st.markdown("#### 📈 Forecast")

    # ── Check credentials ──────────────────────────────────────
    gkey = os.getenv("GOOGLE_API_KEY", "").strip()
    gcreds = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not gkey and not gcreds:
        st.warning(
            "**Google Sheets not connected.** Add one of these to your `.env` file:\n\n"
            "- `GOOGLE_API_KEY=...` *(if the sheet is public — Anyone with link can view)*\n"
            "- `GOOGLE_CREDENTIALS_JSON=/path/to/service-account.json` *(private sheet)*",
            icon="🔑",
        )
        return

    # ── Sync button ────────────────────────────────────────────
    c_sync, c_info = st.columns([1, 6])
    with c_sync:
        if st.button("🔄 Sync Sheet", type="primary", key="forecast_sync"):
            load_forecast.clear()
            st.rerun()

    forecast_data = load_forecast()

    if "_error" in forecast_data:
        st.error(f"Failed to load forecast: {forecast_data['_error']}")
        return
    if not forecast_data:
        st.info("No forecast data found in the Google Sheet.")
        return

    with c_info:
        st.caption(f"Loaded {len(forecast_data)} products from Google Sheet · "
                   "Press **Sync Sheet** to refresh after changes.")

    st.divider()

    # ══════════════════════════════════════════════════════════
    # SECTION 1 — Forecast Viewer
    # ══════════════════════════════════════════════════════════
    st.markdown("##### Section 1 — Forecast Viewer")

    # Product selector
    asin_options = list(forecast_data.keys())
    labels       = {a: f"{a} · {forecast_data[a]['name']}" for a in asin_options}
    selected_asin = st.selectbox(
        "Select product",
        options=asin_options,
        format_func=lambda a: labels[a],
        key="forecast_asin_select",
    )

    if selected_asin:
        product   = forecast_data[selected_asin]
        df        = product["data"].copy()

        # Filter to ≤ Dec 2026 and from today onwards
        today_ym  = date.today().strftime("%Y-%m")
        df        = df[(df.index >= today_ym) & (df.index <= "2026-12")]

        if df.empty:
            st.info("No forecast data for this period.")
        else:
            # Pretty month labels
            def fmt_month(ym: str) -> str:
                try:
                    dt = datetime.strptime(ym, "%Y-%m")
                    return dt.strftime("%b %Y")
                except Exception:
                    return ym

            # Build display: countries as rows, months as columns
            display = df.copy()
            display.columns = ["🇪🇸 ES", "🇫🇷 FR", "🇩🇪 DE", "🇮🇹 IT"]
            display.index = [fmt_month(m) for m in display.index]
            display["TOTAL"] = display.sum(axis=1)
            display = display.astype(int).T   # single transpose → countries=rows, months=cols
            display.index.name = "Country"

            month_cols = list(display.columns)

            # Header cells — light grey bg, bold black text
            header_cells = "".join(
                f'<th style="background:#F4F4F2;color:#111;font-weight:700;'
                f'font-size:12px;padding:9px 12px;text-align:right;'
                f'white-space:nowrap;border-bottom:2px solid #E0E0E0">{c}</th>'
                for c in month_cols
            )

            # Data rows
            html_rows = ""
            for idx, row in display.iterrows():
                is_total = idx == "TOTAL"
                row_bg   = "#FFFBEA" if is_total else ("#FFFFFF" if display.index.get_loc(idx) % 2 == 0 else "#FAFAFA")
                row_fw   = "700"     if is_total else "400"
                row_col  = "#111"    if is_total else "#333"
                border   = "border-top:2px solid #E0E0E0;" if is_total else ""
                idx_cell = (
                    f'<td style="background:#F4F4F2;font-weight:700;color:#111;'
                    f'font-size:12px;padding:9px 12px;white-space:nowrap;'
                    f'border-right:1px solid #E0E0E0;{border}">{idx}</td>'
                )
                data_cells = "".join(
                    f'<td style="background:{row_bg};font-weight:{row_fw};color:{row_col};'
                    f'font-size:13px;padding:9px 12px;text-align:right;{border}">'
                    f'{int(v):,}</td>'
                    for v in row
                )
                html_rows += f'<tr>{idx_cell}{data_cells}</tr>\n'

            forecast_html = f"""
<div style="overflow-x:auto;border-radius:10px;border:1px solid #E8E8E8;
            box-shadow:0 1px 4px rgba(0,0,0,.06)">
<table style="width:100%;border-collapse:collapse;font-family:-apple-system,sans-serif">
  <thead>
    <tr>
      <th style="background:#F4F4F2;color:#111;font-weight:700;font-size:12px;
                 padding:9px 12px;text-align:left;border-bottom:2px solid #E0E0E0;
                 white-space:nowrap">Country</th>
      {header_cells}
    </tr>
  </thead>
  <tbody>{html_rows}</tbody>
</table>
</div>"""
            st.markdown(forecast_html, unsafe_allow_html=True)

    st.divider()

    # ══════════════════════════════════════════════════════════
    # SECTION 2 — Distribution Calculator
    # ══════════════════════════════════════════════════════════
    st.markdown("##### Section 2 — Distribution Calculator")

    import math as _math

    def fmt_m(ym):
        try:
            return datetime.strptime(ym, "%Y-%m").strftime("%b %Y")
        except Exception:
            return ym

    def next_month(ym: str) -> str:
        y, m = int(ym[:4]), int(ym[5:7])
        m += 1
        if m > 12:
            m, y = 1, y + 1
        return f"{y:04d}-{m:02d}"

    # ── Product + config row ───────────────────────────────────
    cfg1, cfg2, cfg3, cfg4 = st.columns([3, 1, 1, 1])
    with cfg1:
        dist_asin = st.selectbox(
            "Product to distribute",
            options=asin_options,
            format_func=lambda a: labels[a],
            key="dist_asin_select",
        )
    with cfg2:
        upb_key = f"upb_{dist_asin}"
        units_per_box = st.number_input(
            "Units / box",
            min_value=1, max_value=500,
            value=st.session_state.get(upb_key, 60),
            step=1, key=f"upb_input_{dist_asin}",
            help="Units per box for this product. Saved per ASIN.",
        )
        st.session_state[upb_key] = units_per_box
    with cfg3:
        n_months = st.selectbox(
            "Forecast months",
            options=[2, 3, 4],
            index=1,           # default = 3
            key="dist_n_months",
            help="How many months of forecast to cover. 3 months recommended — "
                 "accounts for lead time + buffer.",
        )
    with cfg4:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("↩️ Reset", key="dist_reset"):
            for k in list(st.session_state.keys()):
                if k.startswith("dist_") or k == "calc_dist":
                    st.session_state.pop(k, None)
            st.rerun()

    if not dist_asin:
        return

    # ── Units / boxes input ────────────────────────────────────
    col_u, col_b = st.columns(2)
    with col_u:
        total_units = st.number_input(
            "📦 Total units received",
            min_value=0, value=0,
            step=units_per_box, key="dist_units_input",
        )
    with col_b:
        boxes_in = st.number_input(
            f"📦 Or number of boxes ({units_per_box} units/box)",
            min_value=0, value=0,
            step=1, key="dist_boxes_input",
        )

    # Resolve available: units input takes priority, else boxes × upb
    if total_units > 0:
        available       = total_units
        available_boxes = available / units_per_box
    elif boxes_in > 0:
        available       = boxes_in * units_per_box
        available_boxes = boxes_in
    else:
        available       = 0
        available_boxes = 0

    st.caption(
        f"Available: **{available:,} units** "
        f"({available_boxes:.1f} boxes × {units_per_box} units/box)"
    )

    # ── Forecast months ────────────────────────────────────────
    today    = date.today()
    months   = [today.strftime("%Y-%m")]
    for _ in range(n_months - 1):
        months.append(next_month(months[-1]))

    month_labels = " + ".join(fmt_m(m) for m in months)
    st.caption(f"Forecast months used: **{month_labels}**")

    # ── Build per-country data ─────────────────────────────────
    fcast_df = forecast_data[dist_asin]["data"]
    inv_idx  = {r["asin"]: r for r in rows if r["asin"] == dist_asin}
    prod_row = inv_idx.get(dist_asin)

    COUNTRIES = ["ES", "FR", "DE", "IT"]
    FLAGS_MAP = {"ES": "🇪🇸", "FR": "🇫🇷", "DE": "🇩🇪", "IT": "🇮🇹"}

    country_data = {}
    for mp in COUNTRIES:
        fc_total = sum(
            int(fcast_df.loc[m, mp]) if m in fcast_df.index else 0
            for m in months
        )
        c_inv = {}
        if prod_row:
            for c in prod_row.get("countries", []):
                if c["mp"] == mp:
                    c_inv = c
                    break
        stock   = c_inv.get("avail",   0)
        inbound = c_inv.get("inbound", 0)
        vel     = c_inv.get("vel",     0.0)
        need    = max(0, fc_total - stock - inbound)
        country_data[mp] = {
            "flag":    FLAGS_MAP[mp],
            "stock":   stock,
            "inbound": inbound,
            "fc_nm":   fc_total,
            "need":    need,
            "vel":     vel,
            "days":    round(stock / vel, 0) if vel > 0 else 9999,
        }

    # ── Calculate button ───────────────────────────────────────
    if st.button("🧮 Calculate Distribution", type="primary", key="calc_dist"):
        total_need = sum(v["need"] for v in country_data.values())

        if total_need == 0:
            raw_units = {mp: available / 4 for mp in COUNTRIES}
        else:
            weights = {}
            for mp, d in country_data.items():
                urgency = 1.0 / (d["days"] + 1)
                weights[mp] = d["need"] + urgency * total_need * 0.2
            total_w = sum(weights.values())
            raw_units = {mp: available * w / total_w for mp, w in weights.items()}

        # Snap to whole boxes (ceiling), then cap total at available_boxes
        sugg_boxes: dict[str, int] = {}
        for mp in COUNTRIES:
            sugg_boxes[mp] = _math.ceil(raw_units[mp] / units_per_box)

        # If ceiling pushed us over, trim from least-urgent countries
        while sum(sugg_boxes.values()) * units_per_box > available and available_boxes > 0:
            least_urgent = max(COUNTRIES, key=lambda m: country_data[m]["days"]
                               if sugg_boxes[m] > 0 else -1)
            if sugg_boxes[least_urgent] > 0:
                sugg_boxes[least_urgent] -= 1
            else:
                break

        st.session_state["dist_result"] = sugg_boxes

    # ── Output table ───────────────────────────────────────────
    if "dist_result" in st.session_state:
        sugg_boxes = st.session_state["dist_result"]

        st.markdown("**Distribution plan** — adjust boxes per country as needed:")

        edit_rows = []
        for mp in COUNTRIES:
            d         = country_data[mp]
            sb        = sugg_boxes.get(mp, 0)
            su        = sb * units_per_box
            edit_rows.append({
                "Country":         f"{d['flag']} {mp}",
                "Stock":           d["stock"],
                "Inbound":         d["inbound"],
                f"Forecast {n_months}M": d["fc_nm"],
                "Need":            d["need"],
                "Sugg. boxes":     sb,
                "Sugg. units":     su,
                "✏️ Boxes to send": sb,
            })

        edit_df = pd.DataFrame(edit_rows)
        edited  = st.data_editor(
            edit_df,
            column_config={
                "Country":           st.column_config.TextColumn("Country",     disabled=True),
                "Stock":             st.column_config.NumberColumn("Stock",      disabled=True),
                "Inbound":           st.column_config.NumberColumn("Inbound",    disabled=True),
                f"Forecast {n_months}M": st.column_config.NumberColumn(
                    f"Forecast ({month_labels})", disabled=True),
                "Need":              st.column_config.NumberColumn("Need",       disabled=True),
                "Sugg. boxes":       st.column_config.NumberColumn("Sugg. boxes", disabled=True),
                "Sugg. units":       st.column_config.NumberColumn(
                    f"Sugg. units ({units_per_box}/box)", disabled=True),
                "✏️ Boxes to send":  st.column_config.NumberColumn(
                    "✏️ Boxes to send", min_value=0, step=1),
            },
            hide_index=True,
            use_container_width=True,
            key="dist_editor",
        )

        total_boxes_out = int(edited["✏️ Boxes to send"].sum())
        total_units_out = total_boxes_out * units_per_box
        over = total_units_out > available

        def _summary_card(label, boxes, units, color="#111"):
            return f"""
<div style="background:#fff;border:1px solid #E8E8E8;border-radius:8px;
            padding:12px 16px;text-align:center">
  <div style="font-size:11px;color:#888;font-weight:600;text-transform:uppercase;
              letter-spacing:.05em;margin-bottom:6px">{label}</div>
  <div style="font-size:22px;font-weight:800;color:{color};line-height:1.1">{boxes}</div>
  <div style="font-size:11px;color:#aaa;margin-top:2px">{units} units</div>
</div>"""

        rem_boxes = int(available_boxes) - total_boxes_out
        rem_units = available - total_units_out
        sc1, sc2, sc3, sc4 = st.columns(4)
        sc1.markdown(_summary_card("Available",       f"{int(available_boxes)} boxes", f"{available:,}"), unsafe_allow_html=True)
        sc2.markdown(_summary_card("Allocated",       f"{total_boxes_out} boxes", f"{total_units_out:,}"), unsafe_allow_html=True)
        sc3.markdown(_summary_card("Remaining boxes", f"{rem_boxes} boxes", f"{rem_units:,}",
                                   color="#E24B4A" if rem_boxes < 0 else "#3B6D11"), unsafe_allow_html=True)
        sc4.markdown(_summary_card("Units per box",   f"{units_per_box}", "configured"), unsafe_allow_html=True)

        if over:
            st.error(f"⚠️ {total_boxes_out} boxes × {units_per_box} = {total_units_out:,} units "
                     f"exceeds available {available:,}. Reduce by "
                     f"{total_boxes_out - int(available_boxes)} box(es).")
        else:
            result_rows = []
            for _, row_data in edited.iterrows():
                mp       = row_data["Country"].split()[-1]
                d        = country_data[mp]
                b_send   = int(row_data["✏️ Boxes to send"])
                u_send   = b_send * units_per_box
                new_stk  = d["stock"] + d["inbound"] + u_send
                new_days = round(new_stk / d["vel"], 0) if d["vel"] > 0 else 9999
                result_rows.append({
                    "Country":      row_data["Country"],
                    "Boxes":        b_send,
                    "Units":        u_send,
                    "Stock after":  new_stk,
                    "Days after":   int(new_days) if new_days < 9999 else "∞",
                })
            st.markdown("**Result after shipment:**")
            st.dataframe(
                pd.DataFrame(result_rows),
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Country":     st.column_config.TextColumn("Country"),
                    "Boxes":       st.column_config.NumberColumn("Boxes to send"),
                    "Units":       st.column_config.NumberColumn("Units to send"),
                    "Stock after": st.column_config.NumberColumn("Stock after shipment"),
                    "Days after":  st.column_config.TextColumn("Days of stock after"),
                },
            )


# ══════════════════════════════════════════════════════════════
# SETTINGS TAB
# ══════════════════════════════════════════════════════════════

def render_settings():
    st.markdown("### ⚙️ Alert Thresholds")
    st.caption(
        "Configure when each product shows 🔴 critical, 🟡 warning, or 🟢 ok. "
        "Changes apply immediately to the Inventory and Reorder tabs."
    )

    col1, col2 = st.columns(2, gap="large")

    # ── Left: status dot thresholds ──────────────────────────
    with col1:
        with st.container(border=True):
            st.markdown("**Status dot thresholds**")
            st.caption(
                "Compares days-of-stock to each product's own lead time. "
                "🔴 always triggers when days ≤ lead time."
            )
            warn_buf = st.slider(
                "🟡 Warning buffer (days above lead time)",
                min_value=0, max_value=90,
                value=st.session_state.warn_buffer,
                step=1,
                key="warn_buf_slider",
                help="Show 🟡 warning when days of stock ≤ lead time + this buffer. "
                     "Set to 0 to only show critical alerts.",
            )
            st.markdown(
                f"""
<div style="background:#F8F8F8;border-radius:8px;padding:14px 16px;
            margin-top:4px;margin-bottom:12px;font-size:13px;line-height:2.2">
  <span style="display:inline-block;width:10px;height:10px;border-radius:50%;
               background:#E24B4A;margin-right:6px;vertical-align:middle"></span>
  <b>Critical</b> — days of stock &lt; lead time<br>
  <span style="display:inline-block;width:10px;height:10px;border-radius:50%;
               background:#F5A623;margin-right:6px;vertical-align:middle"></span>
  <b>Warning</b> — days of stock &lt; lead time + <b>{warn_buf}d</b><br>
  <span style="display:inline-block;width:10px;height:10px;border-radius:50%;
               background:#4CAF50;margin-right:6px;vertical-align:middle"></span>
  <b>OK</b> — days of stock ≥ lead time + <b>{warn_buf}d</b>
</div>""",
                unsafe_allow_html=True,
            )
            if st.button("✅ Apply alert thresholds", type="primary", key="apply_alert_btn"):
                st.session_state.warn_buffer = warn_buf
                load_master.clear()
                st.rerun()

    # ── Right: days-left column colors ───────────────────────
    with col2:
        with st.container(border=True):
            st.markdown("**Days left column colors**")
            st.caption(
                "Sets the absolute day thresholds for the red/amber/green color coding "
                "in the Days left column (independent of lead time)."
            )
            c_r, c_a = st.columns(2)
            with c_r:
                days_red = st.number_input(
                    "🔴 Red below (days)",
                    min_value=1, max_value=365,
                    value=st.session_state.days_red,
                    key="days_red_input",
                )
            with c_a:
                days_amber = st.number_input(
                    "🟡 Amber below (days)",
                    min_value=1, max_value=365,
                    value=st.session_state.days_amber,
                    key="days_amber_input",
                )
            if days_red >= days_amber:
                st.warning("⚠️ Red threshold must be less than amber threshold.")

            st.markdown(
                f"""
<div style="background:#F8F8F8;border-radius:8px;padding:14px 16px;
            margin-top:4px;margin-bottom:12px;font-size:13px;line-height:2.2">
  <span style="color:#E24B4A;font-weight:700;font-size:16px">■</span>&nbsp;
  <b>Red</b> — days &lt; <b>{days_red}d</b><br>
  <span style="color:#BA7517;font-weight:700;font-size:16px">■</span>&nbsp;
  <b>Amber</b> — days &lt; <b>{days_amber}d</b><br>
  <span style="color:#639922;font-weight:700;font-size:16px">■</span>&nbsp;
  <b>Green</b> — days ≥ <b>{days_amber}d</b>
</div>""",
                unsafe_allow_html=True,
            )
            if st.button("✅ Apply color thresholds", type="primary", key="apply_color_btn",
                         disabled=(days_red >= days_amber)):
                st.session_state.days_red   = days_red
                st.session_state.days_amber = days_amber
                st.rerun()

    st.divider()
    if st.button("↩️ Reset all to defaults", key="reset_settings_btn"):
        st.session_state.warn_buffer = 15
        st.session_state.days_red    = 30
        st.session_state.days_amber  = 60
        load_master.clear()
        st.rerun()

    st.caption("Settings are stored for this browser session only. "
               "Changes to alert thresholds also affect the Reorder Planner tab.")


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

inject_css()

# ── Force sidebar permanently open ───────────────────────────────
st.markdown("""
<script>
(function() {
    // Clear any stored "collapsed" state from localStorage
    try {
        Object.keys(localStorage).forEach(function(k) {
            if (k.toLowerCase().includes('sidebar')) localStorage.removeItem(k);
        });
    } catch(e) {}

    function forceSidebarOpen() {
        // 1. Click the expand button if present
        var btn = document.querySelector('[data-testid="collapsedControl"] button');
        if (btn) btn.click();

        // 2. Force all sidebar elements visible
        var sidebar = document.querySelector('section[data-testid="stSidebar"]');
        if (sidebar) {
            sidebar.style.setProperty('transform',   'none',    'important');
            sidebar.style.setProperty('display',     'block',   'important');
            sidebar.style.setProperty('visibility',  'visible', 'important');
            sidebar.style.setProperty('min-width',   '244px',   'important');
            sidebar.style.setProperty('opacity',     '1',       'important');
            sidebar.setAttribute('aria-expanded', 'true');
            // Also force inner content visible
            var inner = sidebar.querySelector('[data-testid="stSidebarContent"]');
            if (inner) {
                inner.style.setProperty('display',    'block',   'important');
                inner.style.setProperty('visibility', 'visible', 'important');
            }
        }
    }

    // Run on load
    if (document.readyState === 'complete') forceSidebarOpen();
    else window.addEventListener('load', forceSidebarOpen);
    setTimeout(forceSidebarOpen, 300);
    setTimeout(forceSidebarOpen, 800);

    // Watch for any future collapse attempts and immediately undo them
    var observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(m) {
            if (m.target && m.target.dataset &&
                (m.target.dataset.testid === 'stSidebar' ||
                 m.target.dataset.testid === 'stSidebarContent')) {
                forceSidebarOpen();
            }
        });
    });

    function attachObserver() {
        var sidebar = document.querySelector('section[data-testid="stSidebar"]');
        if (sidebar) {
            observer.observe(sidebar, {
                attributes: true,
                subtree: true,
                attributeFilter: ['style', 'aria-expanded', 'class']
            });
            forceSidebarOpen();
        } else {
            setTimeout(attachObserver, 200);
        }
    }
    attachObserver();
})();
</script>
""", unsafe_allow_html=True)

render_sidebar()

# Session state
for k, v in [("mp_filter", list(MARKETPLACES)), ("alert_filter", ["critical","warning","ok"]),
              ("only_critical", False), ("warn_buffer", 15), ("days_red", 30), ("days_amber", 60),
              ("asin_search", ""), ("page", "inventory")]:
    if k not in st.session_state:
        st.session_state[k] = v

rows = load_master(warn_buffer=st.session_state.warn_buffer)

# ── Topbar ─────────────────────────────────────────────────────
st.markdown(f"""
<div style="background:#111;padding:16px 28px;border-radius:10px;margin-bottom:8px;
            display:flex;align-items:center;justify-content:space-between">
  <div style="display:flex;align-items:center;gap:20px">
    {LOGO_IMG}
    <div style="width:1px;height:36px;background:rgba(255,255,255,0.15)"></div>
    <span style="font-size:16px;font-weight:600;color:#fff;letter-spacing:.02em">
      Amazon EU Inventory
    </span>
  </div>
  <div style="font-size:13px;color:#aaa;display:flex;align-items:center;gap:8px">
    <span style="display:inline-block;width:8px;height:8px;border-radius:50%;
                 background:#4CAF50"></span>
    Last sync: 2h ago
  </div>
</div>
""", unsafe_allow_html=True)

_, c_log, c_sync = st.columns([9, 1, 1])
with c_log:
    if st.button("🕐 Log", key="open_sync", help="View sync history"):
        sync_dialog()
with c_sync:
    if st.button("🔄 Sync", type="primary", key="quick_sync"):
        if DEMO_MODE:
            st.toast("Demo mode — connect SP-API to sync", icon="🧪")
        else:
            with st.spinner("Syncing…"):
                try:
                    from backend.fetchers.catalog import fetch_catalog
                    from backend.fetchers.inventory import fetch_all_inventory
                    from backend.fetchers.sales import fetch_all_sales
                    from backend.reorder import calculate_reorder_alerts
                    fetch_catalog(); fetch_all_inventory()
                    fetch_all_sales(); calculate_reorder_alerts()
                    clear_caches(); st.rerun()
                except Exception as e:
                    st.error(f"Sync failed: {e}")

if DEMO_MODE:
    st.info("**Demo mode** — running with sample data. Add credentials to `.env` to go live.", icon="🧪")

st.markdown("<div style='margin-bottom:8px'></div>", unsafe_allow_html=True)

# ── Marketplace cards (always visible) ────────────────────────
render_mp_cards(rows)

# ── Page content driven by sidebar nav ────────────────────────
page = st.session_state.get("page", "inventory")

# ════════ PAGE: INVENTORY ════════
if page == "inventory":
    st.markdown("#### 📦 Inventory", unsafe_allow_html=False)
    fc1, fc2, fc3, fc4, fc5 = st.columns([3, 3, 2, 2, 2])
    with fc1:
        new_mp = st.multiselect("Marketplaces", MARKETPLACES,
                                default=st.session_state.mp_filter,
                                key="mp_select", label_visibility="collapsed",
                                placeholder="All marketplaces")
        if new_mp != st.session_state.mp_filter:
            st.session_state.mp_filter = new_mp; st.rerun()
    with fc2:
        new_al = st.multiselect("Alert", ["critical","warning","ok"],
                                default=st.session_state.alert_filter,
                                key="al_select", label_visibility="collapsed",
                                placeholder="All statuses")
        if new_al != st.session_state.alert_filter:
            st.session_state.alert_filter = new_al; st.rerun()
    with fc3:
        oc = st.toggle("🔴 Critical only", value=st.session_state.only_critical, key="crit_tog")
        if oc != st.session_state.only_critical:
            st.session_state.only_critical = oc; st.rerun()
    with fc4:
        search = st.text_input("Search ASIN", value=st.session_state.asin_search,
                               key="asin_search_input", label_visibility="collapsed",
                               placeholder="🔍 Search ASIN or name…")
        if search != st.session_state.asin_search:
            st.session_state.asin_search = search; st.rerun()
    with fc5:
        st.download_button("⬇️ Export Excel", data=export_excel(rows),
                           file_name=f"nyvos_{datetime.now(timezone.utc).date()}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    active_mps    = st.session_state.mp_filter or MARKETPLACES
    active_alerts = ["critical"] if st.session_state.only_critical else (st.session_state.alert_filter or ["critical","warning","ok"])
    search_q      = st.session_state.asin_search.strip().lower()
    display_rows  = rows
    if search_q:
        display_rows = [r for r in rows
                        if search_q in r["asin"].lower()
                        or search_q in r["name"].lower()
                        or search_q in (r.get("sku") or "").lower()]

    n_vis = len([r for r in display_rows if r["worst"] in active_alerts])
    st.caption(f"{n_vis} of {len(rows)} products · Click any row to expand per-country detail")

    table_html = build_table_html(display_rows, active_mps, active_alerts)
    max_h = 96 + len(display_rows) * 52 + len(display_rows) * 4 * 44 + 40
    components.html(table_html, height=max_h, scrolling=False)
    render_edit_panel(rows)

# ════════ PAGE: REORDER ════════
elif page == "reorder":
    st.markdown("#### 🚚 Reorder Planner", unsafe_allow_html=False)
    render_reorder_planner(rows)

# ════════ PAGE: ORDERS ════════
elif page == "orders":
    render_orders_page(rows)

# ════════ PAGE: FORECAST ════════
elif page == "forecast":
    render_forecast_page(rows)

# ════════ PAGE: SETTINGS ════════
elif page == "settings":
    render_settings()
