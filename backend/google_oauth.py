"""Google OAuth for the Purchase-Orders Drive + Gmail integration.

One-time dance:
    1. María creates an OAuth 2.0 Web client in Google Cloud and pastes
       its client_id + client_secret into the dashboard.
    2. Dashboard builds an auth URL (Drive + Gmail scopes) and shows it.
    3. María clicks → Google consent → Google redirects back to the
       Streamlit app with ?code=…
    4. Dashboard exchanges the code for access_token + refresh_token,
       stores them in po_google_tokens (singleton).

From then on, get_credentials() returns a Credentials object with a
fresh access_token (auto-refreshed) that any Google client library
accepts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from backend.database import db_admin

# Scopes:
#   Drive — upload files to specific folders only (shared or owned)
#   Gmail — create drafts (we never auto-send; María clicks Send in Gmail)
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.compose",
    "openid",
    "email",
]
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"


# ─────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────

def get_google_tokens() -> dict:
    rows = (db_admin.table("po_google_tokens")
            .select("*").eq("id", 1).limit(1).execute().data) or []
    return rows[0] if rows else {}


def save_client(client_id: str, client_secret: str) -> None:
    db_admin.table("po_google_tokens").update({
        "client_id":     client_id.strip(),
        "client_secret": client_secret.strip(),
        "updated_at":    datetime.now(timezone.utc).isoformat(),
    }).eq("id", 1).execute()


def save_tokens(access_token: str, refresh_token: str | None,
                expires_in: int, email: str | None = None) -> None:
    expiry = (datetime.now(timezone.utc) +
              timedelta(seconds=max(0, expires_in - 60))).isoformat()
    data: dict[str, Any] = {
        "access_token": access_token,
        "token_expiry": expiry,
        "scopes":       " ".join(SCOPES),
        "updated_at":   datetime.now(timezone.utc).isoformat(),
    }
    if refresh_token:
        data["refresh_token"] = refresh_token
    if email:
        data["authorized_email"] = email
    db_admin.table("po_google_tokens").update(data).eq("id", 1).execute()


def disconnect() -> None:
    db_admin.table("po_google_tokens").update({
        "refresh_token":   None,
        "access_token":    None,
        "token_expiry":    None,
        "authorized_email": None,
    }).eq("id", 1).execute()


# ─────────────────────────────────────────────────────────────────────
# OAuth flow
# ─────────────────────────────────────────────────────────────────────

def build_auth_url(redirect_uri: str) -> str:
    tok = get_google_tokens()
    client_id = (tok.get("client_id") or "").strip()
    if not client_id:
        raise RuntimeError("Falta Client ID — guárdalo primero en la UI.")
    from urllib.parse import urlencode
    params = {
        "response_type":   "code",
        "client_id":       client_id,
        "redirect_uri":    redirect_uri,
        "scope":           " ".join(SCOPES),
        "access_type":     "offline",     # ensures refresh_token is returned
        "prompt":          "consent",     # force refresh_token every time
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Exchange the ?code=… that Google redirected back with for tokens."""
    import requests
    tok = get_google_tokens()
    client_id     = (tok.get("client_id") or "").strip()
    client_secret = (tok.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("Faltan Client ID / Secret.")

    r = requests.post(TOKEN_URL, data={
        "code":          code,
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  redirect_uri,
        "grant_type":    "authorization_code",
    }, timeout=30)
    r.raise_for_status()
    data = r.json()

    # Pull the email out of the returned id_token (base64url middle part)
    email = None
    id_tok = data.get("id_token")
    if id_tok:
        try:
            import base64, json as _json
            payload = id_tok.split(".")[1] + "=="
            email = _json.loads(base64.urlsafe_b64decode(payload)).get("email")
        except Exception:
            pass

    save_tokens(
        access_token  = data["access_token"],
        refresh_token = data.get("refresh_token"),
        expires_in    = int(data.get("expires_in", 3600)),
        email         = email,
    )
    return data


def _refresh_access_token() -> str:
    """Use the stored refresh_token to get a new access_token."""
    import requests
    tok = get_google_tokens()
    client_id     = (tok.get("client_id") or "").strip()
    client_secret = (tok.get("client_secret") or "").strip()
    refresh       = (tok.get("refresh_token") or "").strip()
    if not refresh:
        raise RuntimeError("Google no está conectado — autoriza desde el tab 'Google'.")

    r = requests.post(TOKEN_URL, data={
        "refresh_token": refresh,
        "client_id":     client_id,
        "client_secret": client_secret,
        "grant_type":    "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    data = r.json()
    save_tokens(
        access_token  = data["access_token"],
        refresh_token = None,                        # refresh token stays
        expires_in    = int(data.get("expires_in", 3600)),
    )
    return data["access_token"]


def get_credentials():
    """Return a google.oauth2.credentials.Credentials object with a
    fresh access_token. Call before building a Drive / Gmail client."""
    from google.oauth2.credentials import Credentials

    tok = get_google_tokens()
    if not tok.get("refresh_token"):
        raise RuntimeError("Google no está conectado — autoriza desde el tab 'Google'.")

    # If the stored access_token is expired or about to expire, refresh it.
    needs_refresh = True
    if tok.get("access_token") and tok.get("token_expiry"):
        try:
            exp = datetime.fromisoformat(tok["token_expiry"])
            needs_refresh = (exp <= datetime.now(timezone.utc))
        except ValueError:
            needs_refresh = True
    if needs_refresh:
        _refresh_access_token()
        tok = get_google_tokens()

    return Credentials(
        token=tok["access_token"],
        refresh_token=tok["refresh_token"],
        token_uri=TOKEN_URL,
        client_id=tok["client_id"],
        client_secret=tok["client_secret"],
        scopes=SCOPES,
    )


def is_connected() -> bool:
    return bool((get_google_tokens() or {}).get("refresh_token"))
