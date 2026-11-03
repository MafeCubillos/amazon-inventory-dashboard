-- Catalogue patch:
--   • Add missing product: MACA NEGRA 50G.
--   • Rename "MACA NEGRA INTERMEDIA F1" → "MACA NEGRA 27G".

update po_products
set    name = 'MACA NEGRA 27G',
       updated_at = now()
where  name = 'MACA NEGRA INTERMEDIA F1';

with sup as (select id, lower(name) as n from po_suppliers)
insert into po_products (name, description, unit_price, moq, default_supplier_id)
select 'MACA NEGRA 50G',
       E'MACA NEGRA 50.000 MG 100:1\n120 CÁPSULAS HPMC 0 TRANSPARENTES\nBOTE PET NEGRO 250 C.C. + TAPA PLÁSTICO\nNEGRA + SLEVER',
       3.50, 1666, sup.id
from   sup
where  sup.n = 'laboratorios bequisa, s.l.'
on conflict (name, default_supplier_id) do nothing;
