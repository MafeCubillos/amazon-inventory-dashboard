-- TikTok Shop Order line items (one row per order_id × sku_id).
-- Populated by backend.fetchers.tiktok_orders_enrich from the Order API.
-- Joins back to tiktok_finance_transactions by order_id to get the revenue
-- and fees for a given sold SKU, and to tiktok_product_map by product_id
-- to resolve ASIN → products.cogs_eur for COGS and margin.

create table if not exists tiktok_order_items (
    id                     bigserial primary key,

    order_id               text        not null,
    sku_id                 text        not null,
    product_id             text,
    product_name           text,
    sku_name               text,
    seller_sku             text,
    quantity               integer,
    original_price         numeric(14, 4),
    sale_price             numeric(14, 4),
    currency               text,

    raw                    jsonb       not null,
    fetched_at             timestamptz not null default now(),

    constraint tiktok_order_items_natural_key
        unique (order_id, sku_id)
);

create index if not exists tiktok_order_items_order_idx
    on tiktok_order_items (order_id);

create index if not exists tiktok_order_items_product_idx
    on tiktok_order_items (product_id);
