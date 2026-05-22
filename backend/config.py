import os
import logging
from dotenv import load_dotenv

load_dotenv()

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── SP-API credentials ────────────────────────────────────────
SP_API_CREDENTIALS = {
    "lwa_app_id":        os.getenv("LWA_APP_ID", ""),
    "lwa_client_secret": os.getenv("LWA_CLIENT_SECRET", ""),
    "refresh_token":     os.getenv("SP_API_REFRESH_TOKEN", ""),
    "aws_access_key":    os.getenv("AWS_ACCESS_KEY_ID", ""),
    "aws_secret_key":    os.getenv("AWS_SECRET_ACCESS_KEY", ""),
}

SELLER_ID = os.getenv("SELLER_ID", "")

# ── Supabase ──────────────────────────────────────────────────
SUPABASE_URL              = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY         = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# ── EU marketplace IDs ────────────────────────────────────────
MARKETPLACES: dict[str, str] = {
    "ES": "A1RKKUPIHCS9HS",
    "FR": "A13V1IB3VIYZZH",
    "DE": "A1PA6795UKMFR9",
    "IT": "APJ6JRA9NG5V4",
}

SYNC_INTERVAL_HOURS = int(os.getenv("SYNC_INTERVAL_HOURS", "6"))
