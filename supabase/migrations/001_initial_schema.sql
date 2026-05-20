-- ============================================================
-- Nyvos Amazon EU Inventory Dashboard — Initial Schema
-- Run this against your Supabase project via the SQL editor or
-- the Supabase CLI:  supabase db push
-- ============================================================

-- ── products ────────────────────────────────────────────────
-- One row per ASIN.  COGS and lead times are managed manually
-- via the dashboard; everything else is synced from SP-API.
CREATE TABLE IF NOT EXISTS products (
    asin                TEXT        PRIMARY KEY,
    sku                 TEXT,
    product_name        TEXT,
    cogs_eur            NUMERIC(10, 2) DEFAULT 0,
    target_days_coverage INT         DEFAULT 60,
    lead_time_days      INT         DEFAULT 30,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Auto-update updated_at on every row change
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER products_updated_at
BEFORE UPDATE ON products
FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ── inventory_snapshots ──────────────────────────────────────
-- Append-only daily snapshots from FBA Inventory API.
-- The latest snapshot per (asin, marketplace) is the live figure.
CREATE TABLE IF NOT EXISTS inventory_snapshots (
    id                  BIGSERIAL   PRIMARY KEY,
    asin                TEXT        NOT NULL REFERENCES products(asin) ON DELETE CASCADE,
    marketplace         TEXT        NOT NULL CHECK (marketplace IN ('ES','FR','DE','IT')),
    units_available     INT         DEFAULT 0,
    units_reserved      INT         DEFAULT 0,
    units_inbound       INT         DEFAULT 0,
    stock_value_eur     NUMERIC(12, 2) DEFAULT 0,
    snapshot_date       DATE        NOT NULL DEFAULT CURRENT_DATE,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_inv_asin_mkt_date
    ON inventory_snapshots (asin, marketplace, snapshot_date DESC);

-- Unique constraint so we upsert safely (one snapshot per day per asin+marketplace)
CREATE UNIQUE INDEX IF NOT EXISTS idx_inv_upsert
    ON inventory_snapshots (asin, marketplace, snapshot_date);


-- ── sales_velocity ───────────────────────────────────────────
-- Rolling sales windows written after each sync.
-- velocity_daily = units_sold_30d / 30  (computed in Python, stored here)
CREATE TABLE IF NOT EXISTS sales_velocity (
    id                  BIGSERIAL   PRIMARY KEY,
    asin                TEXT        NOT NULL REFERENCES products(asin) ON DELETE CASCADE,
    marketplace         TEXT        NOT NULL CHECK (marketplace IN ('ES','FR','DE','IT')),
    units_sold_7d       INT         DEFAULT 0,
    units_sold_14d      INT         DEFAULT 0,
    units_sold_30d      INT         DEFAULT 0,
    velocity_daily      NUMERIC(10, 4) DEFAULT 0,
    period_end_date     DATE        NOT NULL DEFAULT CURRENT_DATE,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vel_asin_mkt_date
    ON sales_velocity (asin, marketplace, period_end_date DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_vel_upsert
    ON sales_velocity (asin, marketplace, period_end_date);


-- ── reorder_alerts ───────────────────────────────────────────
-- Computed after each sync. One row per (asin, marketplace).
-- Upserted on every run so values are always fresh.
CREATE TABLE IF NOT EXISTS reorder_alerts (
    id                      BIGSERIAL   PRIMARY KEY,
    asin                    TEXT        NOT NULL REFERENCES products(asin) ON DELETE CASCADE,
    marketplace             TEXT        NOT NULL CHECK (marketplace IN ('ES','FR','DE','IT')),
    days_of_stock_left      NUMERIC(10, 2),
    days_until_reorder      NUMERIC(10, 2),
    alert_status            TEXT        CHECK (alert_status IN ('critical','warning','ok')),
    suggested_reorder_qty   INT         DEFAULT 0,
    calculated_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_upsert
    ON reorder_alerts (asin, marketplace);

CREATE INDEX IF NOT EXISTS idx_alert_status
    ON reorder_alerts (alert_status);


-- ── sync_log ─────────────────────────────────────────────────
-- Append-only audit trail written at the end of every sync job.
CREATE TABLE IF NOT EXISTS sync_log (
    id              BIGSERIAL   PRIMARY KEY,
    sync_type       TEXT        NOT NULL CHECK (sync_type IN ('inventory','sales','catalog','reorder')),
    marketplace     TEXT        CHECK (marketplace IN ('ES','FR','DE','IT')),
    status          TEXT        NOT NULL CHECK (status IN ('success','error','partial')),
    records_updated INT         DEFAULT 0,
    error_message   TEXT,
    synced_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_synclog_synced_at
    ON sync_log (synced_at DESC);


-- ── Row Level Security ───────────────────────────────────────
-- Enable RLS on all tables.
-- The backend uses the service-role key (bypasses RLS).
-- The Streamlit app uses the anon key; restrict it to SELECT only.
ALTER TABLE products           ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_velocity      ENABLE ROW LEVEL SECURITY;
ALTER TABLE reorder_alerts      ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_log            ENABLE ROW LEVEL SECURITY;

-- Allow anon/authenticated to read all tables
CREATE POLICY "anon_read_products"            ON products            FOR SELECT USING (true);
CREATE POLICY "anon_read_inventory_snapshots" ON inventory_snapshots FOR SELECT USING (true);
CREATE POLICY "anon_read_sales_velocity"      ON sales_velocity      FOR SELECT USING (true);
CREATE POLICY "anon_read_reorder_alerts"      ON reorder_alerts      FOR SELECT USING (true);
CREATE POLICY "anon_read_sync_log"            ON sync_log            FOR SELECT USING (true);

-- Allow authenticated users (dashboard) to update products (COGS / lead times)
CREATE POLICY "auth_update_products"          ON products            FOR UPDATE USING (true);
