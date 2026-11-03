-- CC list for Gmail drafts (comma-separated). Lives on the company
-- profile so María can add/remove recipients from the Datos de empresa
-- tab without redeploying.

alter table po_company_profile
    add column if not exists cc_emails text;

update po_company_profile
set    cc_emails = 'carlos.zuniga@monvita.co'
where  id = 1 and (cc_emails is null or cc_emails = '');
