-- Manual monthly values for the Finance-TikTok P&L — things that
-- aren't in the TikTok API or the Excel: shipments from Monvita's
-- warehouse to TikTok (UPS/Bequisa), agency management fees, and
-- external TikTok Ads spend (ads manager outside the GMV payments).
--
-- One row per (month, field). Edited inline from the dashboard.

create table if not exists tiktok_finance_manual (
    id          bigserial primary key,
    month       text        not null,       -- YYYY-MM
    field       text        not null,       -- e.g. 'shipping_total', 'agency_fee', 'tiktok_ads_external'
    value_eur   numeric(14, 2) not null default 0,
    note        text,
    updated_at  timestamptz not null default now(),

    constraint tiktok_finance_manual_natural_key unique (month, field)
);

create index if not exists tiktok_finance_manual_month_idx
    on tiktok_finance_manual (month);
