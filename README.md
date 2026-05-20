# Nyvos Amazon EU Inventory Dashboard

Live inventory and sales visibility across **ES · FR · DE · IT** Amazon marketplaces, powered by the Amazon Selling Partner API and Supabase.

```
SP-API ──► Python backend (fetcher / scheduler) ──► Supabase (PostgreSQL) ──► Streamlit dashboard
```

---

## Project Structure

```
amazon-inventory-dashboard/
├── .env.example                   # Credential template — copy to .env
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── supabase/
│   └── migrations/
│       └── 001_initial_schema.sql # Run once against your Supabase project
├── backend/
│   ├── config.py                  # Env-var loading, marketplace IDs
│   ├── database.py                # Supabase client wrappers
│   ├── reorder.py                 # Alert & reorder-qty calculation
│   ├── scheduler.py               # APScheduler entry point
│   └── fetchers/
│       ├── inventory.py           # FBA Inventory API
│       ├── sales.py               # Reports API (GET_SALES_AND_TRAFFIC_REPORT)
│       └── catalog.py             # Catalog Items API
└── dashboard/
    └── app.py                     # Streamlit frontend
```

---

## 1 · Prerequisites

| Tool | Version |
|------|---------|
| Python | 3.11+ |
| pip | latest |
| Supabase account | free tier works |
| Amazon Seller Central account | EU unified account |

---

## 2 · Amazon SP-API Setup

### 2.1 Register a developer application

1. Go to **Seller Central → Apps & Services → Develop Apps**.
2. Click **Add new app client** and fill in the form.
3. Under **OAuth redirect URI**, enter any placeholder (e.g. `https://localhost`).
4. Note down your **LWA Client ID** and **LWA Client Secret**.

### 2.2 Create an IAM user (AWS Console)

1. Open **IAM → Users → Create user**.
2. Attach the policy below (inline or managed):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "execute-api:Invoke",
      "Resource": "arn:aws:execute-api:eu-west-1:*:*"
    }
  ]
}
```

3. Create **Access keys** → **Application running outside AWS**.
4. Save the **Access Key ID** and **Secret Access Key**.

### 2.3 Authorize the application & get a refresh token

1. In Seller Central, go to **Apps & Services → Manage Apps → Authorize new app**.
2. Enter your LWA Client ID, then follow the OAuth flow.
3. Capture the `spapi_oauth_code` from the redirect URL.
4. Exchange it for a refresh token:

```bash
curl -X POST https://api.amazon.com/auth/o2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code" \
  -d "code=YOUR_SPAPI_OAUTH_CODE" \
  -d "redirect_uri=https://localhost" \
  -d "client_id=YOUR_LWA_CLIENT_ID" \
  -d "client_secret=YOUR_LWA_CLIENT_SECRET"
```

Save the `refresh_token` from the response — it does not expire.

---

## 3 · Supabase Setup

1. Create a project at [supabase.com](https://supabase.com).
2. Go to **Project Settings → API** and note:
   - **Project URL**
   - **anon / public** key
   - **service_role** key (keep secret — backend writes only)
3. Open the **SQL Editor** and run the migration:

```bash
# Option A — paste the file content into Supabase SQL Editor
cat supabase/migrations/001_initial_schema.sql

# Option B — Supabase CLI
supabase db push
```

---

## 4 · Local Setup

```bash
# 1. Clone / navigate to the project
cd amazon-inventory-dashboard

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure credentials
cp .env.example .env
# Edit .env with your real keys
```

---

## 5 · Running

### Option A — two terminals (development)

**Terminal 1 — background sync scheduler:**
```bash
python -m backend.scheduler
```

Runs a full sync immediately on start, then every `SYNC_INTERVAL_HOURS` hours (default: 6).

**Terminal 2 — Streamlit dashboard:**
```bash
streamlit run dashboard/app.py
```

Open [http://localhost:8501](http://localhost:8501).

### Option B — Docker Compose

```bash
docker compose up --build
```

Dashboard available at [http://localhost:8501](http://localhost:8501).

---

## 6 · Sync Pipeline

Each run executes in this order:

| Step | API | Table written |
|------|-----|---------------|
| Catalog | Catalog Items API | `products` (name, SKU) |
| Inventory | FBA Inventory API | `inventory_snapshots` |
| Sales | Reports API (`GET_SALES_AND_TRAFFIC_REPORT`) | `sales_velocity` |
| Reorder | Computed from Supabase data | `reorder_alerts`, `inventory_snapshots.stock_value_eur` |

All syncs are logged to `sync_log`. Errors are retried with exponential back-off (up to 5 attempts).

> **Sales report latency:** Amazon's async Reports API typically takes 2–10 minutes to generate a 30-day daily report. The scheduler handles the polling automatically.

---

## 7 · Dashboard Features

### Inventory Overview tab
- Per-product table with all 4 marketplaces side-by-side
- Inline editing of **COGS (€)**, **lead time (days)**, and **target coverage (days)** — saved directly to Supabase
- Alert badges 🔴🟡🟢 per marketplace
- Filter by marketplace, alert status, or toggle "critical only"
- Export current view to **Excel**

### Reorder Planner tab
- Shows only products with critical / warning status
- Editable **target days of coverage** — suggested reorder qty recalculates live
- Export reorder plan to **Excel**

### Sync Status tab
- Last sync time per marketplace
- Full sync history from `sync_log`
- **Sync Now** button for manual trigger

---

## 8 · Reorder Logic

| Metric | Formula |
|--------|---------|
| `velocity_daily` | `units_sold_30d ÷ 30` |
| `days_of_stock_left` | `units_available ÷ velocity_daily` |
| `days_until_reorder` | `days_of_stock_left − lead_time_days` |
| `suggested_reorder_qty` | `max(0, (target_coverage − days_left) × velocity_daily)` |

| `days_of_stock_left` | Status |
|---------------------|--------|
| `< lead_time_days` | 🔴 critical |
| `< lead_time_days + 15` | 🟡 warning |
| otherwise | 🟢 ok |

---

## 9 · EU Marketplace IDs

| Code | Marketplace ID |
|------|---------------|
| ES | A1RKKUPIHCS9HS |
| FR | A13V1IB3VIYZZH |
| DE | A1PA6795UKMFR9 |
| IT | APJ6JRA9NG5V4 |

---

## 10 · Security Notes

- The **service-role key** is used only by the backend (Python). Never expose it in the frontend.
- The **anon key** is safe to use in Streamlit — Supabase RLS restricts it to `SELECT` only.
- All credentials live in `.env` — add `.env` to `.gitignore`.

```bash
echo ".env" >> .gitignore
```
