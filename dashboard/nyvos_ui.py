"""Nyvos design-system UI helpers.

Shared HTML table components used across pages (same dark header look as
Amazon Inventory). Both app.py and purchase_orders_page.py import from here.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


_TBL_COUNTER = {"n": 0}


def nyvos_table_html(columns: list[dict], rows: list[dict]) -> str:
    """Build sortable HTML table with Nyvos dark header (black bg + gold text).

    columns: list of {"key": str, "label": str, "width": str, "align": str, "sort_type": str}
    rows:    list of dicts keyed by `key`; values are already-formatted strings (HTML allowed).
             Pass "<key>__sort" in rows for numeric sort override.
    """
    _TBL_COUNTER["n"] += 1
    tid = f"nyvos_tbl_{_TBL_COUNTER['n']}"

    ths = ""
    for i, c in enumerate(columns):
        stype = c.get("sort_type", "num" if c.get("align") == "right" else "text")
        ths += (
            f'<th onclick="nyvosSort_{tid}({i},\'{stype}\')" '
            f'style="width:{c.get("width","auto")};text-align:{c.get("align","left")};'
            f'color:#E8C879;padding:11px 10px;font-size:11.5px;font-weight:800;'
            f'text-transform:uppercase;letter-spacing:0.08em;white-space:nowrap;'
            f'background:#111111;position:sticky;top:0;z-index:10;'
            f'cursor:pointer;user-select:none;'
            f'box-shadow:0 1px 0 rgba(255,255,255,0.08)">'
            f'{c["label"]} <span id="{tid}_si_{i}" style="font-size:9px;opacity:0.5">⇅</span></th>'
        )

    body = ""
    for i, r in enumerate(rows):
        bg = "#FFFFFF" if i % 2 == 0 else "#FBFAF7"
        tds = ""
        for c in columns:
            v = r.get(c["key"], "")
            sv = r.get(c["key"] + "__sort", "")
            data_attr = f' data-sort="{sv}"' if sv != "" else ""
            tds += (
                f'<td{data_attr} style="padding:10px 10px;text-align:{c.get("align","left")};'
                f'font-size:12.5px;color:#111;border-bottom:1px solid #EEE;'
                f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
                f'{v}</td>'
            )
        body += (f'<tr style="background:{bg}" '
                 f'onmouseover="this.style.background=\'#F5F5F0\'" '
                 f'onmouseout="this.style.background=\'{bg}\'">{tds}</tr>')

    sort_js = f"""
<script>
window.nyvosSort_{tid}_dir = window.nyvosSort_{tid}_dir || {{}};
function nyvosSort_{tid}(colIdx, kind) {{
    var tbl = document.getElementById('{tid}');
    if (!tbl) return;
    var tbody = tbl.tBodies[0];
    var rows = Array.from(tbody.rows);
    var prev = window.nyvosSort_{tid}_dir[colIdx] || 0;
    var dir = prev === 1 ? -1 : 1;
    window.nyvosSort_{tid}_dir = {{}};
    window.nyvosSort_{tid}_dir[colIdx] = dir;
    rows.sort(function(a, b) {{
        var ca = a.cells[colIdx], cb = b.cells[colIdx];
        var va = ca.dataset.sort !== undefined ? ca.dataset.sort : ca.innerText.trim();
        var vb = cb.dataset.sort !== undefined ? cb.dataset.sort : cb.innerText.trim();
        if (kind === 'num') {{
            var na = parseFloat(String(va).replace(/[^0-9.\\-]/g,'')) || 0;
            var nb = parseFloat(String(vb).replace(/[^0-9.\\-]/g,'')) || 0;
            return (na - nb) * dir;
        }}
        return va.localeCompare(vb) * dir;
    }});
    rows.forEach(function(r, i) {{
        var bg = i % 2 === 0 ? '#FFFFFF' : '#FBFAF7';
        r.style.background = bg;
        r.setAttribute('onmouseover', "this.style.background='#F5F5F0'");
        r.setAttribute('onmouseout', "this.style.background='" + bg + "'");
        tbody.appendChild(r);
    }});
    var ths = tbl.tHead.rows[0].cells;
    for (var k = 0; k < ths.length; k++) {{
        var si = document.getElementById('{tid}_si_' + k);
        if (si) {{
            si.innerText = (k === colIdx) ? (dir === 1 ? '▲' : '▼') : '⇅';
            si.style.opacity = (k === colIdx) ? '1' : '0.5';
        }}
    }}
}}
</script>"""

    return (
        sort_js +
        f'<div style="overflow:auto;max-height:620px;border-radius:10px;'
        f'border:1px solid #E8E8E8;background:white;box-shadow:0 1px 4px rgba(0,0,0,.06);margin-bottom:12px">'
        f'<table id="{tid}" style="width:100%;border-collapse:collapse;table-layout:auto;font-family:Inter,sans-serif">'
        f'<thead><tr>{ths}</tr></thead><tbody>{body}</tbody></table></div>'
    )


def render_nyvos_df(df: pd.DataFrame, widths: dict | None = None,
                    aligns: dict | None = None, formatters: dict | None = None,
                    max_height: int = 620) -> None:
    """Render a DataFrame as a sortable Nyvos HTML table."""
    widths = widths or {}
    aligns = aligns or {}
    formatters = formatters or {}
    numeric_cols = set(df.select_dtypes(include="number").columns)

    columns = []
    for c in df.columns:
        align = aligns.get(c, "right" if c in numeric_cols else "left")
        columns.append({
            "key":   c,
            "label": str(c),
            "width": widths.get(c, "auto"),
            "align": align,
            "sort_type": "num" if c in numeric_cols else "text",
        })

    def _safe_num(v):
        try:
            if v is None:
                return None
            fv = float(v)
            if fv != fv:
                return None
            return fv
        except Exception:
            return None

    rows = []
    for _, r in df.iterrows():
        row_d = {}
        for c in df.columns:
            raw = r[c]
            if c in formatters:
                val = formatters[c](raw)
            elif c in numeric_cols:
                n = _safe_num(raw)
                if n is None:
                    val = "—"
                elif n == int(n):
                    val = f"{int(n):,}"
                else:
                    val = f"{n:,.2f}"
            else:
                val = "" if raw is None or (isinstance(raw, float) and raw != raw) else str(raw)
            row_d[c] = val
            if c in numeric_cols:
                n = _safe_num(raw)
                if n is not None:
                    row_d[c + "__sort"] = str(n)
        rows.append(row_d)

    components.html(
        nyvos_table_html(columns=columns, rows=rows),
        height=min(max_height, 90 + 38 * max(1, len(rows))),
        scrolling=True,
    )
