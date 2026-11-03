-- ASIN mapping on po_products: a single product in the catalog can
-- have multiple ASINs (one per marketplace: amazon.es, amazon.fr,
-- amazon.it, amazon.de…). The auto-draft job uses this to match a
-- reorder_alerts row (keyed by asin) back to a product in the catalog
-- and figure out which supplier to order from.

alter table po_products
    add column if not exists asins text[] default '{}'::text[];

create index if not exists po_products_asins_gin
    on po_products using gin (asins);
