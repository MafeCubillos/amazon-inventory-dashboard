-- Google OAuth state + per-product Drive folder mapping.
--
-- po_google_tokens is a singleton (one Google Workspace account per
-- dashboard). It holds the OAuth refresh_token so background jobs
-- (uploading POs to Drive, drafting emails) run as María's monvita.co
-- account without re-auth.

create table if not exists po_google_tokens (
    id              int         primary key default 1,
    client_id       text,
    client_secret   text,
    refresh_token   text,
    access_token    text,
    token_expiry    timestamptz,
    scopes          text,
    authorized_email text,
    updated_at      timestamptz not null default now(),
    constraint po_google_tokens_singleton check (id = 1)
);

insert into po_google_tokens (id) values (1)
on conflict (id) do nothing;

-- Each product can point to its own Drive folder for storing PO PDFs.
alter table po_products
    add column if not exists drive_folder_id text;

-- Seed folder IDs from María's existing Drive layout.
update po_products set drive_folder_id = '1MGaWOni2bYPE5o9uI_mdhP7VqAFlB2uM' where name = 'ASHWAGANDHA BLEND';
update po_products set drive_folder_id = '1hZ2VvJKGu3SHYIDaWbY10NxTc8Q2Sj84' where name = 'ASHWAGANDHA PLUS';
update po_products set drive_folder_id = '1AgYWVlgZUKA3b-D3at08RiQX1CJHoWEs' where name = 'ASHWAGANDHA ULTRA';
update po_products set drive_folder_id = '10zEVSRFxoMTBkYCP2oQrPnaIHJjx-70i' where name = 'ESPINO AMARILLO + BIOTINA + ZINC';
update po_products set drive_folder_id = '1KqXkf2Al_4rbD6-KAbVyyHqJoDxRWKA8' where name = 'MACA NEGRA 27G';
update po_products set drive_folder_id = '1PyJORGKHxXvwDhNBKVS7UEq_hklDaR0r' where name = 'MACA NEGRA 45G';
update po_products set drive_folder_id = '1vNcd1j3zu_e_zaRHeyKk2LdJZxRRK3pj' where name = 'MACA NEGRA 50G';
update po_products set drive_folder_id = '1yMda4R-MVAJUArTyAEOz_Y_C4t8vyik9' where name = 'MACA NEGRA Y TRÍBULUS';
update po_products set drive_folder_id = '1VNgznxJLtQg0MvwQA659QG7EUIWa_hqC' where name = 'MACA ROJA 25.000';
update po_products set drive_folder_id = '1uoeQKo2x9GSJBpu6vDprDlbOk418btKg' where name = 'NITROBEET';
update po_products set drive_folder_id = '1rqGctX48WRNx1s21yRLK2XA8aacwkbFL' where name = 'SAFFRON COMPLEX';
update po_products set drive_folder_id = '1qW9jlCqPrkqjrs_ioMI9wAQBVS9j3QzF' where name = 'SAW PALMETTO HAIR';
update po_products set drive_folder_id = '13dxjKfg_ElFvVaS-5JugFRiH2a3YlHNR' where name = 'SEROCLAR CAFEINA NATURAL';
update po_products set drive_folder_id = '1LTT21IBzUxJEWtouaXyCqrJggfQ3_WZS' where name = 'SHATAVARI BALANCE COMPLEX';

-- Save Drive uploads + Gmail draft refs on the PO itself (already in 006
-- as drive_file_id / gmail_draft_id — nothing to add here).
