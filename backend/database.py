"""Supabase client helpers.

Two clients are created at module load time:
  - `db`       — anon key (read-only, safe for the dashboard)
  - `db_admin` — service-role key (full write access for the backend)
"""

import logging
from supabase import create_client, Client
from backend.config import SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY

logger = logging.getLogger(__name__)

db: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
db_admin: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ── Convenience wrappers ──────────────────────────────────────

def upsert(table: str, rows: list[dict], on_conflict: str) -> int:
    """Upsert a list of dicts and return the number of affected rows."""
    if not rows:
        return 0
    resp = db_admin.table(table).upsert(rows, on_conflict=on_conflict).execute()
    count = len(resp.data) if resp.data else 0
    logger.debug("upsert %s → %d rows", table, count)
    return count


def insert(table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    resp = db_admin.table(table).insert(rows).execute()
    count = len(resp.data) if resp.data else 0
    logger.debug("insert %s → %d rows", table, count)
    return count


def log_sync(
    sync_type: str,
    marketplace: str | None,
    status: str,
    records_updated: int = 0,
    error_message: str | None = None,
) -> None:
    row = {
        "sync_type":        sync_type,
        "marketplace":      marketplace,
        "status":           status,
        "records_updated":  records_updated,
        "error_message":    error_message,
    }
    db_admin.table("sync_log").insert(row).execute()
