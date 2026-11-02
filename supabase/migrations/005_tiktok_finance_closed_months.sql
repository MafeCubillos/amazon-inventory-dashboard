-- Months whose Finance data was imported from Excel are "closed":
-- Sync from TikTok must not overwrite them, since the Excel is the
-- authoritative source for closed months.
--
-- Writing a row here = "this month is final, don't touch it".
-- Deleting a row here = "reopen for Sync to overwrite".

create table if not exists tiktok_finance_closed_months (
    month       text        primary key,       -- YYYY-MM
    closed_at   timestamptz not null default now(),
    source      text        not null default 'excel',  -- 'excel' or 'manual'
    note        text
);
