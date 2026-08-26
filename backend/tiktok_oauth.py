"""OAuth exchange helpers for TikTok Shop Partner API.

Docs: https://partner.tiktokshop.com/docv2/page/64f199d75f00bf02da4d6087

Custom app flow (single-shop):
  1. Merchant visits an authorize URL and approves the app
  2. TikTok redirects back to our Redirect URL with ?code=XXX
  3. Backend exchanges the code for access_token + refresh_token
  4. Along with tokens we get shop_cipher — required on every subsequent API call

We only need to do steps 3-4 in Python. Steps 1-2 happen in the browser.
"""

from __future__ import annotations

import os
import httpx
from typing import Any

_TOKEN_URL = "https://auth.tiktok-shops.com/api/v2/token/get"
_REFRESH_URL = "https://auth.tiktok-shops.com/api/v2/token/refresh"


def build_authorize_url(app_key: str, state: str = "nyvos") -> str:
    """URL the merchant visits to grant the app access to their shop."""
    return (
        "https://services.tiktokshop.com/open/authorize"
        f"?app_key={app_key}&state={state}"
    )


def exchange_code_for_token(app_key: str, app_secret: str, auth_code: str) -> dict[str, Any]:
    """Exchange a one-time authorization code for an access_token bundle.

    Returns the raw response dict on success. Expected keys inside data:
      access_token, refresh_token, access_token_expire_in, refresh_token_expire_in,
      open_id, seller_name, shop_id/shop_cipher (naming varies by version)
    Raises on non-2xx or non-zero API code.
    """
    r = httpx.get(_TOKEN_URL, params={
        "app_key":    app_key,
        "app_secret": app_secret,
        "auth_code":  auth_code,
        "grant_type": "authorized_code",
    }, timeout=15)
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"TikTok token exchange failed: {payload}")
    return payload.get("data", {})


def refresh_access_token(app_key: str, app_secret: str, refresh_token: str) -> dict[str, Any]:
    """Renew an access_token before it expires (30-day validity)."""
    r = httpx.get(_REFRESH_URL, params={
        "app_key":       app_key,
        "app_secret":    app_secret,
        "refresh_token": refresh_token,
        "grant_type":    "refresh_token",
    }, timeout=15)
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"TikTok token refresh failed: {payload}")
    return payload.get("data", {})
