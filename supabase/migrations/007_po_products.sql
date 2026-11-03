-- Product catalog for Purchase Orders — the items María orders
-- regularly from Bequisa / Natucap. Lets the dashboard prefill
-- description + unit price + MOQ when she picks a product in a PO.

create table if not exists po_products (
    id                  bigserial   primary key,
    name                text        not null,              -- short name shown in dropdowns
    description         text        not null,              -- full composition / packaging (goes to PDF)
    unit_price          numeric(14, 4) not null default 0,
    moq                 numeric(14, 3) not null default 0, -- default quantity on new PO
    default_supplier_id bigint      references po_suppliers(id),
    active              boolean     not null default true,
    notes               text,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),

    constraint po_products_name_supplier_unique unique (name, default_supplier_id)
);

create index if not exists po_products_supplier_idx on po_products (default_supplier_id);
create index if not exists po_products_active_idx   on po_products (active) where active;

-- Seed from Nyvos product sheet. Match supplier case-insensitively so
-- 'Natucap S.A.' in the sheet lines up with 'NATUCAP S.A.' in po_suppliers.
with sup as (
    select id, lower(name) as n from po_suppliers
),
rows(name, description, unit_price, moq, supplier_name) as (values
    (E'MACA NEGRA 45G', E'MACA NEGRA 45G\n180 CÁPSULAS HPMC 0 TRANSPARENTE\nBOTE PET NEGRO 500 C.C. + TAPA PLÁSTICO\nNEGRA + SLEVER',                                                                                     4.72::numeric,  1111::numeric, 'laboratorios bequisa, s.l.'),
    (E'MACA NEGRA INTERMEDIA F1', E'MACA NEGRA INTERMEDIA F1\n180 CÁPSULAS HPMC 0 TRANSPARENTE\nBOTE PET NEGRO 300 C.C. + TAPA PLÁSTICO NEGRA + SLEVER',                                                                4.90::numeric,   500::numeric, 'laboratorios bequisa, s.l.'),
    (E'ASHWAGANDHA PLUS', E'ASHWAGANDHA PLUS: ASHWAGANDHA 10% + 2,5%\n120 CÁPSULAS HPMC 0 TRANSPARENTE\nBOTE PET NEGRO 200 C.C. + TAPA PLÁSTICO NEGRA + SLEVER',                                                        5.24::numeric,   500::numeric, 'laboratorios bequisa, s.l.'),
    (E'MACA ROJA 25.000', E'MACA ROJA 25.000\n180 CÁPSULAS HPMC 0 TRANSPARENTE\nBOTE PET NEGRO 250 C.C. + TAPA PLÁSTICO NEGRA + SLEVER',                                                                                4.93::numeric,   666::numeric, 'laboratorios bequisa, s.l.'),
    (E'SHATAVARI BALANCE COMPLEX', E'SHATAVARI BALANCE COMPLEX\n120 CÁPSULAS HPMC 0 TRANSPARENTE\nBOTE PET NEGRO 250 C.C. + TAPA PLÁSTICO NEGRA + SLEVER',                                                              5.50::numeric,  1040::numeric, 'laboratorios bequisa, s.l.'),
    (E'SAFFRON COMPLEX', E'SAFFRON COMPLEX\n120 CÁPSULAS HPMC 1 TRANSPARENTE\nBOTE PET BLANCO 250 C.C. + TAPA PLÁSTICO BLANCA + SLEVER',                                                                                6.49::numeric,   500::numeric, 'laboratorios bequisa, s.l.'),
    (E'SEROCLAR CAFEINA NATURAL', E'SEROCLAR CAFEINA NATURAL\n90 CÁPSULAS HPMC 00 TRANSPARENTE\nBOTE PET NEGRO 250 C.C. + TAPA PLÁSTICO NEGRA + SLEVER',                                                                7.06::numeric,   500::numeric, 'laboratorios bequisa, s.l.'),
    (E'ESPINO AMARILLO + BIOTINA + ZINC', E'ESPINO AMARILLO + BIOTINA + ZINC\n180 PERLAS\nBOTE PET BLANCO 250 C.C. + TAPA PLÁSTICO BLANCA + SLEVER',                                                                   4.98::numeric,   554::numeric, 'laboratorios bequisa, s.l.'),
    (E'MACA NEGRA Y TRÍBULUS', E'MACA NEGRA Y TRÍBULUS\n120 CÁPSULAS HPMC 0 TRANSPARENTE\nBOTE PET NEGRO 250 C.C. + TAPA PLÁSTICO NEGRA + SLEVER',                                                                      3.99::numeric,   500::numeric, 'laboratorios bequisa, s.l.'),
    (E'ASHWAGANDHA BLEND', E'ASHWAGANDHA BLEND: ASHWAGANDHA KSM BLEND\n60 CÁPSULAS HPMC 0 TRANSPARENTE\nBOTE PET NEGRO 150 C.C. + TAPA PLÁSTICO NEGRA + SLEVER',                                                        4.12::numeric,   694::numeric, 'laboratorios bequisa, s.l.'),
    (E'NITROBEET', E'NITROBEET\n180 CÁPSULAS HPMC 0 TRANSPARENTE\nBOTE PET NEGRO 300 C.C. + TAPA PLÁSTICO NEGRA + SLEVER',                                                                                              5.61::numeric,  1111::numeric, 'laboratorios bequisa, s.l.'),
    (E'NITROBEET', E'NITROBEET\n180 CÁPSULAS HPMC 0 TRANSPARENTE\nBOTE PET NEGRO 300 C.C. + TAPA PLÁSTICO NEGRA + SLEVER\nExtracto seco de remolacha (Beta vulgaris) (raíz) 25:\n1 - 300 mg\n8% betaína - 24 mg\nAgente de carga (calcio fosfato dihidrato) 143 mg\nAgente de carga (celulosa microcristalina) 55 mg\nAntiaglomerante (sales magnésicas de ácidos\ngrasos) 3,72mg\nColecalciferol 2mg\nVitamina D 0,005mg\nClorhidrato de piridoxina - 1,28 mg\nVitamina B6 - 1,05 mg\nCianocobalamina\nVitamina B12 - 0,00375 mg',                                                       5.61::numeric,  2000::numeric, 'natucap s.a.'),
    (E'ASHWAGANDHA ULTRA', E'ASHWAGANDHA ULTRA: ASHWAGANDHA SHODEN + 2,5%\n120 CÁPSULAS HPMC 0 TRANSPARENTE\nBOTE PET NEGRO 200 C.C. + TAPA PLÁSTICO NEGRA + SLEVER',                                                   7.01::numeric,   695::numeric, 'laboratorios bequisa, s.l.'),
    (E'SAW PALMETTO HAIR', E'SAW PALMETTO HAIR\n120 CÁPSULAS HPMC 0 TRANSPARENTES\nBOTE PET BLANCO 250 ML + TAPA PLÁSTICO BLANCA + SLEVER',                                                                             5.67::numeric,   415::numeric, 'laboratorios bequisa, s.l.')
)
insert into po_products (name, description, unit_price, moq, default_supplier_id)
select r.name, r.description, r.unit_price, r.moq, sup.id
from   rows r
join   sup  on sup.n = r.supplier_name
on conflict (name, default_supplier_id) do nothing;
