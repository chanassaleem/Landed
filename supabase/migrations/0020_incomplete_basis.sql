-- A charge can land in full and still land in the wrong place.
--
-- 0019 asked one question of each charge: is the basis it divides by
-- zero? If not, the whole bill reaches the goods and there is nothing
-- to report. That question is necessary and it is not sufficient.
--
-- Reproduced on a clean database. One shipment, two received lines,
-- 1,000 USD of freight to be divided by weight. Reagent A is entered
-- as 500 kg. Reagent B's weight is never entered.
--
--   the diagnostic said:  billed 280,000  allocated 280,000
--                         unallocated 0   nothing missing
--
--   where it actually went:  A  280,000   landed 3,800 a unit
--                            B        0   landed 1,000 a unit
--
-- The shipment's weight is 500 kg because one line was weighed, so
-- the denominator is not zero and the division succeeds. Every rupee
-- of the freight is then loaded onto Reagent A, which is overvalued
-- by 2,800 a unit, while Reagent B -- which weighs something, in
-- reality -- carries none of it and is undervalued by its true share.
--
-- This is the worse kind of wrong. A charge that reaches nothing is
-- at least visible as a figure that has not moved. This one produces
-- a complete-looking allocation, a confident unit cost, and a screen
-- saying everything is fine, and it feeds the ABC class, the service
-- target, the safety stock and the price list.
--
-- So the question becomes: does every line on this shipment have the
-- figure this charge divides by? Weight and volume live on the
-- shipment line and are nullable, which is where the hole is. Value
-- and quantity come from the receipt line itself, where both columns
-- are not null -- a line priced at zero contributes nothing to a
-- value split, and that is correct rather than missing, because free
-- goods really do attract no duty by value.

create or replace view public.shipment_charge_allocation
with (security_invoker = true) as
  with lines as (
    select sl.org_id, sl.shipment_id,
           count(*)                                        as lines,
           count(*) filter (where sl.weight_kg is null)     as no_weight,
           count(*) filter (where sl.volume_cbm is null)    as no_volume
      from public.shipment_lines sl
      join public.grn_lines_live gl on gl.id = sl.grn_line_id
     group by sl.org_id, sl.shipment_id
  )
  select c.org_id,
         c.shipment_id,
         s.reference,
         s.status                      as shipment_status,
         c.id                          as charge_id,
         c.kind,
         c.allocate_by,
         c.amount,
         c.currency,
         c.fx_rate,
         c.amount * c.fx_rate          as billed,
         case when coalesce(t.qty, 0) = 0 then 0
              when c.allocate_by = 'value'
                   and coalesce(t.fob_value, 0) = 0  then 0
              when c.allocate_by = 'weight'
                   and coalesce(t.weight_kg, 0) = 0  then 0
              when c.allocate_by = 'volume'
                   and coalesce(t.volume_cbm, 0) = 0 then 0
              else c.amount * c.fx_rate
         end                           as allocated,
         case when coalesce(t.qty, 0) = 0 then c.amount * c.fx_rate
              when c.allocate_by = 'value'
                   and coalesce(t.fob_value, 0) = 0  then c.amount * c.fx_rate
              when c.allocate_by = 'weight'
                   and coalesce(t.weight_kg, 0) = 0  then c.amount * c.fx_rate
              when c.allocate_by = 'volume'
                   and coalesce(t.volume_cbm, 0) = 0 then c.amount * c.fx_rate
              else 0
         end                           as unallocated,
         case when coalesce(t.qty, 0) = 0 then 'awaiting goods'
              when c.allocate_by = 'value'
                   and coalesce(t.fob_value, 0) = 0  then 'no goods value'
              when c.allocate_by = 'weight'
                   and coalesce(t.weight_kg, 0) = 0  then 'no weight recorded'
              when c.allocate_by = 'volume'
                   and coalesce(t.volume_cbm, 0) = 0 then 'no volume recorded'
         end                           as blocked_by,

         coalesce(l.lines, 0)          as lines,
         -- How many of them lack the figure this charge divides by.
         case c.allocate_by
           when 'weight' then coalesce(l.no_weight, 0)
           when 'volume' then coalesce(l.no_volume, 0)
           else 0
         end                           as lines_missing_basis,
         -- Landed in full, but split across only some of the goods.
         -- Distinct from blocked_by on purpose: a charge that reached
         -- nothing and a charge that reached the wrong items need
         -- different words and different work to put right.
         case when coalesce(l.lines, 0) = 0 then null
              when c.allocate_by = 'weight' and coalesce(l.no_weight, 0) > 0
                   and coalesce(t.weight_kg, 0) > 0
                then 'weight missing on ' || l.no_weight || ' of '
                     || l.lines || ' lines'
              when c.allocate_by = 'volume' and coalesce(l.no_volume, 0) > 0
                   and coalesce(t.volume_cbm, 0) > 0
                then 'volume missing on ' || l.no_volume || ' of '
                     || l.lines || ' lines'
         end                           as skewed_by
    from public.shipment_costs c
    join public.shipments s on s.id = c.shipment_id
    left join public.shipment_totals t
           on t.shipment_id = c.shipment_id and t.org_id = c.org_id
    left join lines l
           on l.shipment_id = c.shipment_id and l.org_id = c.org_id;

comment on view public.shipment_charge_allocation is
  'One row per charge on a shipment: billed, allocated, unallocated, '
  'and two different ways of being wrong. blocked_by means the basis '
  'was zero and nothing landed. skewed_by means the basis was missing '
  'on some of the lines, so the whole charge landed on the rest -- a '
  'complete-looking allocation and a confident unit cost that is too '
  'high on the weighed goods and too low on the others.';
