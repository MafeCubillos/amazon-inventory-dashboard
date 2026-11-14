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

    # Compute cumulative left offsets for sticky columns (so a 2nd sticky
    # column sits right after the 1st without overlapping).
    sticky_offsets: dict[int, str] = {}
    cur_left = 0
    for i, c in enumerate(columns):
        if c.get("sticky"):
            sticky_offsets[i] = f"{cur_left}px"
            # Parse width ("130px" → 130), default 150 if auto/unknown
            w = c.get("width", "150px")
            try:
                cur_left += int(str(w).replace("px", "").strip())
            except Exception:
                cur_left += 150

    def _sticky_style(i: int, is_header: bool, bg: str) -> str:
        if i not in sticky_offsets:
            return ""
        z = 12 if is_header else 5
        shadow = ("box-shadow:2px 0 4px -2px rgba(0,0,0,0.15),"
                  "0 1px 0 rgba(255,255,255,0.08);"
                  if i == max(sticky_offsets.keys()) else "")
        return (f"position:sticky;left:{sticky_offsets[i]};z-index:{z};"
                f"background:{bg} !important;{shadow}")

    ths = ""
    for i, c in enumerate(columns):
        stype = c.get("sort_type", "num" if c.get("align") == "right" else "text")
        # Headers need BOTH sticky-top AND sticky-left (for columns marked sticky)
        if i in sticky_offsets:
            pos_rule = (f"position:sticky;top:0;left:{sticky_offsets[i]};"
                        f"z-index:20;")
            extra_shadow = ("box-shadow:2px 0 4px -2px rgba(0,0,0,0.25),"
                            "0 1px 0 rgba(255,255,255,0.08);"
                            if i == max(sticky_offsets.keys()) else
                            "box-shadow:0 1px 0 rgba(255,255,255,0.08);")
        else:
            pos_rule = "position:sticky;top:0;z-index:10;"
            extra_shadow = "box-shadow:0 1px 0 rgba(255,255,255,0.08);"
        ths += (
            f'<th onclick="nyvosSort_{tid}({i},\'{stype}\')" '
            f'style="width:{c.get("width","auto")};text-align:{c.get("align","left")};'
            f'color:#E8C879;padding:11px 10px;font-size:11.5px;font-weight:800;'
            f'text-transform:uppercase;letter-spacing:0.08em;white-space:nowrap;'
            f'background:#111111;{pos_rule}'
            f'cursor:pointer;user-select:none;{extra_shadow}">'
            f'{c["label"]} <span id="{tid}_si_{i}" style="font-size:9px;opacity:0.5">⇅</span></th>'
        )

    body = ""
    for i, r in enumerate(rows):
        bg = "#FFFFFF" if i % 2 == 0 else "#FBFAF7"
        tds = ""
        for j, c in enumerate(columns):
            v = r.get(c["key"], "")
            sv = r.get(c["key"] + "__sort", "")
            data_attr = f' data-sort="{sv}"' if sv != "" else ""
            sticky = _sticky_style(j, is_header=False, bg=bg)
            tds += (
                f'<td{data_attr} class="nyvos-cell" '
                f'style="padding:10px 10px;text-align:{c.get("align","left")};'
                f'font-size:12.5px;color:#111;border-bottom:1px solid #EEE;'
                f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'
                f'background:{bg};{sticky}">'
                f'{v}</td>'
            )
        body += (f'<tr data-bg="{bg}" style="background:{bg}" '
                 f'onmouseover="nyvosHoverRow(this, \'#F5F5F0\')" '
                 f'onmouseout="nyvosHoverRow(this, this.dataset.bg)">{tds}</tr>')

    sort_js = f"""
<script>
function nyvosHoverRow(tr, color) {{
    // Hover every td in the row (sticky cells override row bg, so we must set each)
    for (var k = 0; k < tr.cells.length; k++) {{
        tr.cells[k].style.background = color;
    }}
}}
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
        r.dataset.bg = bg;
        // Re-apply bg on each cell (sticky cells need it explicitly)
        for (var k = 0; k < r.cells.length; k++) {{
            r.cells[k].style.background = bg;
        }}
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
        f'<table id="{tid}" style="width:100%;border-collapse:separate;border-spacing:0;'
        f'table-layout:auto;font-family:Inter,sans-serif">'
        f'<thead><tr>{ths}</tr></thead><tbody>{body}</tbody></table></div>'
    )


def render_nyvos_df(df: pd.DataFrame, widths: dict | None = None,
                    aligns: dict | None = None, formatters: dict | None = None,
                    sticky: list | None = None,
                    max_height: int = 620) -> None:
    """Render a DataFrame as a sortable Nyvos HTML table.

    sticky: list of column names to freeze to the left on horizontal scroll.
            Auto-detects ["ASIN", "Product"] if present and sticky is None.
    """
    widths = widths or {}
    aligns = aligns or {}
    formatters = formatters or {}
    if sticky is None:
        sticky = [c for c in ("ASIN", "Product") if c in df.columns]
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
            "sticky":    c in sticky,
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
