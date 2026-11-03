-- Purchase Orders — mirrors Monvita's current Google-Sheets template
-- (po #, date, supplier, line items, subtotal, IVA, total) so María can
-- generate the same PDF from the dashboard instead of editing a sheet.
--
-- NOTE: tables are prefixed `po_` because the Amazon reorder-alerts
-- system already has its own `purchase_orders` table with a different
-- schema. Using distinct names keeps them from colliding.

-- ── Company profile (singleton row) ─────────────────────────────────
create table if not exists po_company_profile (
    id          int         primary key default 1,
    name        text        not null,
    address_lines text      not null,
    cif         text        not null,
    phone       text,
    email       text,
    currency    text        not null default 'EUR',
    constraint  po_company_singleton check (id = 1)
);

insert into po_company_profile (id, name, address_lines, cif, email)
values (
    1,
    'Monvita Wellness Group S.L.',
    E'C/ TRAFALGAR, 17, 5oC\n28010 MADRID\nMadrid',
    'B75335224',
    'mafe.cubillos@monvita.co'
)
on conflict (id) do nothing;

-- ── Suppliers ───────────────────────────────────────────────────────
create table if not exists po_suppliers (
    id            bigserial   primary key,
    name          text        not null unique,
    address_lines text,
    nif           text,
    phone         text,
    web           text,
    email         text,
    drive_folder_id text,
    notes         text,
    created_at    timestamptz not null default now()
);

insert into po_suppliers (name, address_lines, nif, phone, web)
values ('Laboratorios Bequisa, S.L.',
        E'Avda. de las Canteras, 49, nave 604\n28343 Valdemoro\nMadrid',
        'B80975246', '918082488', 'www.laboratoriosbequisa.com')
on conflict (name) do nothing;

insert into po_suppliers (name, address_lines, nif)
values ('NATUCAP S.A.',
        E'P.I. Consuegra\nCalle 3, No15\n45700 Consuegra\nToledo (ES) España',
        'A45907219')
on conflict (name) do nothing;

-- ── Purchase orders ─────────────────────────────────────────────────
create table if not exists po_orders (
    id            bigserial   primary key,
    po_number     int         not null unique,
    po_date       date        not null default current_date,
    supplier_id   bigint      not null references po_suppliers(id),
    invoice_ref   text,
    iva_rate      numeric(5,2) not null default 10.00,
    currency      text        not null default 'EUR',
    status        text        not null default 'draft',
    pdf_bytes     bytea,
    drive_file_id text,
    gmail_draft_id text,
    notes         text,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

create index if not exists po_orders_date_idx on po_orders (po_date desc);
create index if not exists po_orders_supplier_idx on po_orders (supplier_id);

insert into po_orders (po_number, po_date, supplier_id, status, notes)
select 86, '2026-11-03', (select id from po_suppliers limit 1),
       'cancelled', 'Placeholder: last manual PO before the dashboard — do not use.'
where not exists (select 1 from po_orders where po_number = 86);

-- ── Line items ──────────────────────────────────────────────────────
create table if not exists po_order_items (
    id            bigserial   primary key,
    po_id         bigint      not null references po_orders(id) on delete cascade,
    position      int         not null default 1,
    description   text        not null,
    quantity      numeric(14, 3) not null default 0,
    unit_price    numeric(14, 4) not null default 0,
    line_total    numeric(14, 2) generated always as
                  (round((quantity * unit_price)::numeric, 2)) stored
);

create index if not exists po_order_items_po_idx on po_order_items (po_id);
