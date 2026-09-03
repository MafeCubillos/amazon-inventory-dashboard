"""TikTok token management — reads/writes tokens from Supabase so the
app can auto-refresh on 401 without human intervention.

Fallback chain:
  1. tiktok_tokens table (Supabase) — the source of truth after migration
  2. env vars TIKTOK_ACCESS_TOKEN etc. — bootstrap / migration source

When a call receives 401 "Expired credentials", the caller invokes
`refresh_and_persist()` which:
  1. Calls the OAuth refresh endpoint using the current refresh_token
  2. Writes the new access_token / refresh_token back to Supabase
  3. Returns the new access_token so the caller can retry

The Supabase table has a CHECK constraint so only one row can ever exist
(there's only one seller shop). All operations are keyed as singleton.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta

from backend.database import db_admin

logger = logging.getLogger(__name__)


def get_tokens() -> dict:
    """Return {access_token, refresh_token, shop_cipher, ...} — from Supabase
    if present, else from env vars. Missing fields are empty strings, never
    KeyError, so callers can grep for missing pieces cleanly."""
    # 1. Try Supabase
    try:
        rows = db_admin.table("tiktok_tokens").select("*").execute().data or []
        if rows:
            row = rows[0]
            return {
                "access_token":      (row.get("access_token")  or "").strip(),
                "refresh_token":     (row.get("refresh_token") or "").strip(),
                "shop_cipher":       (row.get("shop_cipher")   or "").strip(),
                "access_expires_at": row.get("access_expires_at"),
                "source":            "supabase",
            }
    except Exception as exc:
        logger.warning("tiktok_tokens  Supabase read failed: %s", exc)

    # 2. Fallback to env
    return {
        "access_token":      os.getenv("TIKTOK_ACCESS_TOKEN",  "").strip(),
        "refresh_token":     os.getenv("TIKTOK_REFRESH_TOKEN", "").strip(),
        "shop_cipher":       os.getenv("TIKTOK_SHOP_CIPHER",   "").strip(),
        "access_expires_at": None,
        "source":            "env",
    }


def save_tokens(access_token: str, refresh_token: str, shop_cipher: str,
                access_expires_in_sec: int | None = None) -> bool:
    """Upsert the singleton row in tiktok_tokens."""
    try:
        expires_at = None
        if access_expires_in_sec:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=int(access_expires_in_sec))
            ).isoformat()
        db_admin.table("tiktok_tokens").upsert({
            "singleton":         True,
            "access_token":      access_token,
            "refresh_token":     refresh_token,
            "shop_cipher":       shop_cipher,
            "access_expires_at": expires_at,
            "updated_at":        datetime.now(timezone.utc).isoformat(),
        }, on_conflict="singleton").execute()
        return True
    except Exception as exc:
        logger.error("tiktok_tokens  save failed: %s", exc)
        return False


def refresh_and_persist() -> str:
    """Call TikTok's refresh endpoint, persist the new tokens in Supabase,
    and return the new access_token. Raises on failure so the caller can
    decide whether to fall back to the old token or surface an error."""
    from backend.tiktok_oauth import refresh_access_token

    app_key    = os.getenv("TIKTOK_APP_KEY", "").strip()
    app_secret = os.getenv("TIKTOK_APP_SECRET", "").strip()
    if not (app_key and app_secret):
        raise RuntimeError("TIKTOK_APP_KEY / TIKTOK_APP_SECRET missing")

    current = get_tokens()
    refresh_token = current.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("No refresh_token available — re-authorize via OAuth flow")

    data = refresh_access_token(app_key, app_secret, refresh_token)
    new_access  = (data.get("access_token")  or "").strip()
    new_refresh = (data.get("refresh_token") or refresh_token).strip()
    expires_in  = data.get("access_token_expire_in") or data.get("access_token_expires_in")

    if not new_access:
        raise RuntimeError(f"Refresh returned no access_token: {data}")

    save_tokens(new_access, new_refresh, current.get("shop_cipher", ""),
                access_expires_in_sec=expires_in)
    logger.info("tiktok_tokens  refreshed successfully (expires_in=%s)", expires_in)
    return new_access
