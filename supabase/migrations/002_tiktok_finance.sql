-- TikTok Shop Finance: transaction-level data (equivalent to the "Order details"
-- sheet of the Excel reports downloaded from Finance Center → Statements).
--
-- Each row = one line in Order details (a sale, refund, adjustment, bill
-- deduction, etc.). Fields follow TikTok's own naming, snake_cased.
-- `raw` keeps the full API payload so new fields can be backfilled without
-- re-fetching from TikTok.

create table if not exists tiktok_finance_transactions (
    id                                              bigserial primary key,

    -- Natural keys from TikTok ---------------------------------------------
    statement_id                                    text        not null,
    order_adjustment_id                             text        not null,
    type                                            text        not null,
    related_order_id                                text,

    -- Context --------------------------------------------------------------
    shop_cipher                                     text        not null,
    statement_date                                  date,
    payment_id                                      text,
    status                                          text,
    currency                                        text,
    sku_id                                          text,
    quantity                                        integer,
    product_name                                    text,
    sku_name                                        text,
    order_created_date                              date,
    order_delivery_date                             date,

    -- Totals (settlement) --------------------------------------------------
    total_settlement_amount                         numeric(14, 4),
    net_sales                                       numeric(14, 4),
    subtotal_before_discounts                       numeric(14, 4),
    seller_discounts                                numeric(14, 4),
    refund_subtotal_before_seller_discounts         numeric(14, 4),
    refund_of_seller_discounts                      numeric(14, 4),

    -- Shipping -------------------------------------------------------------
    shipping                                        numeric(14, 4),
    tiktok_shop_shipping_fee                        numeric(14, 4),
    fulfilled_by_tiktok_shop_shipping_fee           numeric(14, 4),
    customer_shipping_fee                           numeric(14, 4),
    refunded_customer_shipping_fee                  numeric(14, 4),
    tiktok_shop_shipping_incentive                  numeric(14, 4),
    tiktok_shop_shipping_incentive_refund           numeric(14, 4),
    shipping_subsidy                                numeric(14, 4),
    actual_return_shipping_fee                      numeric(14, 4),
    shipping_fee_discount                           numeric(14, 4),
    return_shipping_label_fee                       numeric(14, 4),
    return_shipping_fee_reimbursement               numeric(14, 4),
    fee_for_fbt_free_shipping                       numeric(14, 4),

    -- Fees -----------------------------------------------------------------
    fees                                            numeric(14, 4),
    tiktok_shop_commission_fee                      numeric(14, 4),
    affiliate_commission                            numeric(14, 4),
    affiliate_partner_commission                    numeric(14, 4),
    affiliate_shop_ads_commission                   numeric(14, 4),
    epr_pay_on_behalf_service_fee                   numeric(14, 4),
    co_funded_promotion_seller_funded               numeric(14, 4),
    smart_promotion_fee_tax                         numeric(14, 4),
    affiliate_commission_deposit                    numeric(14, 4),
    affiliate_commission_refund                     numeric(14, 4),
    affiliate_partner_shop_ads_commission           numeric(14, 4),
    campaign_resource_fee                           numeric(14, 4),
    tax_and_duty                                    numeric(14, 4),
    smart_promotion_fee                             numeric(14, 4),
    campaign_service_fee                            numeric(14, 4),
    managed_service_plan_sales_tax                  numeric(14, 4),
    managed_service_plan_per_order_fee              numeric(14, 4),
    gmv_max_ad_fee                                  numeric(14, 4),
    auto_post_shoppable_video_commission_fee        numeric(14, 4),

    -- Adjustments ----------------------------------------------------------
    adjustment_amount                               numeric(14, 4),
    adjustment_reason                               text,

    -- Customer flows -------------------------------------------------------
    customer_payment                                numeric(14, 4),
    customer_refund                                 numeric(14, 4),
    seller_co_funded_voucher_discount               numeric(14, 4),
    seller_co_funded_voucher_discount_refund        numeric(14, 4),
    platform_discounts                              numeric(14, 4),
    platform_discounts_refund                       numeric(14, 4),
    platform_co_funded_voucher_discounts            numeric(14, 4),
    platform_co_funded_voucher_discounts_refund     numeric(14, 4),
    customer_paid_shipping_fee_before_discounts     numeric(14, 4),
    seller_shipping_fee_discount                    numeric(14, 4),
    tiktok_shop_shipping_fee_discount_to_customer   numeric(14, 4),

    -- Logistics & misc -----------------------------------------------------
    estimated_chargeable_package_weight             numeric(14, 4),
    chargeable_package_weight                       numeric(14, 4),
    collection_methods                              text,
    delivery_option                                 text,
    mode_of_tiktok_shop_shipping_fee_discount       text,

    -- Full API payload for forward compatibility ---------------------------
    raw                                             jsonb       not null,
    fetched_at                                      timestamptz not null default now(),

    -- Prevent dupes on re-run; one row per (statement, line, type)
    constraint tiktok_finance_tx_natural_key
        unique (statement_id, order_adjustment_id, type)
);

create index if not exists tiktok_finance_tx_date_idx
    on tiktok_finance_transactions (statement_date desc);

create index if not exists tiktok_finance_tx_sku_idx
    on tiktok_finance_transactions (sku_id);

create index if not exists tiktok_finance_tx_shop_idx
    on tiktok_finance_transactions (shop_cipher);
