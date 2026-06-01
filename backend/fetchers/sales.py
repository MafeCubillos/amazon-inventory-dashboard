"""Fetch per-ASIN sales data via the SP-API Reports API.

Report type: GET_SALES_AND_TRAFFIC_REPORT
  - dateGranularity = DAY  (one row per ASIN per day)
  - asinGranularity = CHILD

We request a 30-day report, download the TSV, then aggregate:
  units_sold_7d  = sum of last 7 days
  units_sold_14d = sum of last 14 days
  units_sold_30d = sum of last 30 days
  velocity_daily = units_sold_30d / 30

The report is async: create → poll → download.
"""

import io
import gzip
import logging
import time
from datetime import date, timedelta

import pandas as pd
import requests
from sp_api.api import Reports
from sp_api.base import Marketplaces
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from backend.config import SP_API_CREDENTIALS, MARKETPLACES
from backend.database import upsert, log_sync

logger = logging.getLogger(__name__)

_MARKETPLACE_ENUM = {
    "ES": Marketplaces.ES,
    "FR": Marketplaces.FR,
    "DE": Marketplaces.DE,
    "IT": Marketplaces.IT,
}

_POLL_INTERVAL_SEC = 15
_POLL_MAX_ATTEMPTS = 40  # 10 minutes max wait


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _create_report(api: Reports, start: date, end: date) -> str:
    resp = api.create_report(
        reportType="GET_SALES_AND_TRAFFIC_REPORT",
        dataStartTime=start.isoformat() + "T00:00:00Z",
        dataEndTime=end.isoformat()   + "T23:59:59Z",
        reportOptions={
            "dateGranularity":  "DAY",
            "asinGranularity":  "CHILD",
        },
    )
    return resp.payload["reportId"]


def _poll_report(api: Reports, report_id: str) -> str:
    for attempt in range(_POLL_MAX_ATTEMPTS):
        time.sleep(_POLL_INTERVAL_SEC)
        resp = api.get_report(reportId=report_id)
        status = resp.payload.get("processingStatus", "")
        logger.debug("report %s  status=%s  attempt=%d", report_id, status, attempt + 1)

        if status == "DONE":
            return resp.payload["reportDocumentId"]
        if status in ("FATAL", "CANCELLED"):
            raise RuntimeError(f"Report {report_id} ended with status {status}")

    raise TimeoutError(f"Report {report_id} did not complete in time")


def _download_report(api: Reports, doc_id: str) -> str:
    resp = api.get_report_document(reportDocumentId=doc_id)
    url = resp.payload["url"]
    compression = resp.payload.get("compressionAlgorithm", "")

    raw = requests.get(url, timeout=120).content
    if compression == "GZIP":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8")


def _parse_and_aggregate(report_text: str, today: date) -> pd.DataFrame:
    """Parse GET_SALES_AND_TRAFFIC_REPORT (returned as JSON by Amazon SP-API).

    The report has two sections:
    - salesAndTrafficByDate: daily market-level totals
    - salesAndTrafficByAsin: 30-day per-ASIN totals (what we need)
    """
    import json as _json

    # Handle both JSON and legacy TSV formats
    stripped = report_text.strip()
    if stripped.startswith("{"):
        data = _json.loads(stripped)
        # Lowercase all keys for consistency
        by_asin = data.get("salesAndTrafficByAsin", data.get("salesandtrafficbyasin", []))
        rows = []
        for entry in by_asin:
            asin = entry.get("childAsin", entry.get("childasin", ""))
            if not asin:
                continue
            sales = entry.get("salesByAsin", entry.get("salesbyasin", {}))
            units_30d = int(sales.get("unitsOrdered", sales.get("unitsordered", 0)) or 0)
            rows.append({"asin": asin, "units_sold_30d": units_30d})
        if not rows:
            raise ValueError("No salesAndTrafficByAsin data in JSON report")
        agg = pd.DataFrame(rows)
    else:
        # Legacy TSV path
        df = pd.read_csv(io.StringIO(stripped), sep="\t", low_memory=False)
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
        asin_col  = next((c for c in df.columns if c in ("child_asin", "asin")), None)
        units_col = next((c for c in df.columns if "units_ordered" in c), None)
        if not (asin_col and units_col):
            raise ValueError(f"Unexpected TSV columns: {list(df.columns)}")
        agg = df.groupby(asin_col)[units_col].sum().reset_index()
        agg.columns = ["asin", "units_sold_30d"]
        agg["units_sold_30d"] = agg["units_sold_30d"].fillna(0).astype(int)

    agg["units_sold_7d"]  = (agg["units_sold_30d"] * 7 / 30).round(0).astype(int)
    agg["units_sold_14d"] = (agg["units_sold_30d"] * 14 / 30).round(0).astype(int)
    agg["velocity_daily"] = (agg["units_sold_30d"] / 30).round(4)
    return agg


def fetch_sales_for_marketplace(marketplace_code: str) -> int:
    today = date.today()
    # Amazon's Sales & Traffic Report has ~48h reporting lag.
    # End the window 2 days ago to ensure complete data, then label it as
    # the "30-day" window. Start = end - 29 → exactly 30 days inclusive.
    end   = today - timedelta(days=2)
    start = end   - timedelta(days=29)
    mp_enum = _MARKETPLACE_ENUM[marketplace_code]

    api = Reports(credentials=SP_API_CREDENTIALS, marketplace=mp_enum)

    logger.info("sales  %s  creating report %s → %s (30d window, ends 2d ago to avoid lag)",
                marketplace_code, start, end)
    report_id = _create_report(api, start, end)
    doc_id    = _poll_report(api, report_id)
    tsv_text  = _download_report(api, doc_id)

    df = _parse_and_aggregate(tsv_text, today)

    # Only include ASINs that exist in products table (FK constraint)
    from backend.database import db_admin
    known = {r["asin"] for r in db_admin.table("products").select("asin").execute().data or []}

    rows = [
        {
            "asin":             row["asin"],
            "marketplace":      marketplace_code,
            "units_sold_7d":    int(row["units_sold_7d"]),
            "units_sold_14d":   int(row["units_sold_14d"]),
            "units_sold_30d":   int(row["units_sold_30d"]),
            "velocity_daily":   float(row["velocity_daily"]),
            "period_end_date":  today.isoformat(),
        }
        for _, row in df.iterrows()
        if row["asin"] in known
    ]

    count = upsert("sales_velocity", rows, "asin,marketplace,period_end_date")
    logger.info("sales  %s  → %d records", marketplace_code, count)
    return count


def fetch_all_sales() -> dict[str, int]:
    results: dict[str, int] = {}
    for code in MARKETPLACES:
        try:
            n = fetch_sales_for_marketplace(code)
            log_sync("sales", code, "success", n)
            results[code] = n
        except Exception as exc:
            logger.error("sales  %s  FAILED: %s", code, exc)
            log_sync("sales", code, "error", 0, str(exc))
            results[code] = 0
    return results
