-- A landed cost should say whether it can be trusted.
--
-- 0020 made a skewed allocation visible -- but only on the freight
-- screen, in a column somebody has to go and look at. The unit cost
-- itself said nothing, and the unit cost is what the rest of the
-- product reads.
--
-- Reproduced on a clean database. One shipment, two lines of 100,
-- 1,000 USD freight at 280 to be split by weight, one line weighed
-- and one not:
--
--   stock screen, Landed each:   B-1  3,800.00     B-2  1,000.00
--
-- Both figures are presented the same way as a cost that was divided
-- by a complete basis. B-1 is overvalued by 2,800 a unit and B-2
-- undervalued by its true share, and nothing anywhere in the number
-- says so. Those two figures then set the margin, the price floor,
-- the ABC class and the stock valuation.
--
-- The point is not to hide the figure. An importer needs a working
-- cost the day the goods land, long before the last weight note
-- arrives, and a blank would be worse than an approximate number.
-- The point is that the figure has to carry its own warning, so a
-- price set off it is set knowingly.
--
-- Two different kinds of untrustworthy, and both count:
--
--   blocked_by  the charge divided by zero, so none of it reached
--               these goods -- the cost is too low by the whole bill
--   skewed_by   the charge divided by a basis some lines lacked, so
--               all of it reached the rest -- the cost is too high
--               on the measured lines and too low on the others
--
-- A charge sitting on a shipment whose goods have not arrived is
-- neither: there is no landed cost yet to be provisional about.

create or replace view public.landed_cost with (security_invoker = true) as
  with doubt as (
    -- One row per shipment that has a charge we cannot divide
    -- properly, and the reasons, in the words the freight screen
    -- already uses so the two screens agree.
    select a.org_id,
           a.shipment_id,
           string_agg(distinct coalesce(a.blocked_by, a.skewed_by), '; '
                      order by coalesce(a.blocked_by, a.skewed_by))
             as why
      from public.shipment_charge_allocation a
     where (a.blocked_by is not null or a.skewed_by is not null)
       -- 'awaiting goods' is not doubt about a cost; it is the absence
       -- of one. Excluded by name rather than by testing qty again, so
       -- that this stays in step with 0020 if that wording changes.
       and coalesce(a.blocked_by, '') <> 'awaiting goods'
     group by a.org_id, a.shipment_id
  )
  select b.org_id,
         b.grn_line_id,
         b.item_id,
         b.qty,
         b.fob_value,
         coalesce(sum(
           c.amount * c.fx_rate *
           case c.allocate_by
             when 'value'  then b.fob_value  / nullif(t.fob_value, 0)
             when 'weight' then b.weight_kg  / nullif(t.weight_kg, 0)
             when 'volume' then b.volume_cbm / nullif(t.volume_cbm, 0)
             when 'qty'    then b.qty        / nullif(t.qty, 0)
           end
         ), 0) as allocated_cost,
         (b.fob_value + coalesce(sum(
           c.amount * c.fx_rate *
           case c.allocate_by
             when 'value'  then b.fob_value  / nullif(t.fob_value, 0)
             when 'weight' then b.weight_kg  / nullif(t.weight_kg, 0)
             when 'volume' then b.volume_cbm / nullif(t.volume_cbm, 0)
             when 'qty'    then b.qty        / nullif(t.qty, 0)
           end
         ), 0)) / nullif(b.qty, 0) as landed_unit_cost,
         -- Every line on the shipment, not only the ones short of a
         -- figure. The measured line is the one carrying the whole
         -- charge, so its cost is the more wrong of the two, and a
         -- flag on the unmeasured line alone would point the reader
         -- at the cheaper mistake.
         (d.shipment_id is not null) as provisional,
         d.why                       as why_provisional
    from public.shipment_line_basis b
    join public.shipment_totals t
      on t.shipment_id = b.shipment_id and t.org_id = b.org_id
    left join public.shipment_costs c
      on c.shipment_id = b.shipment_id
    left join doubt d
      on d.shipment_id = b.shipment_id and d.org_id = b.org_id
   group by b.org_id, b.grn_line_id, b.item_id, b.qty, b.fob_value,
            d.shipment_id, d.why;

comment on view public.landed_cost is
  'A received line''s cost with its share of the shipment''s charges. '
  'provisional is true when some charge on that shipment could not be '
  'divided properly -- it reached none of the goods, or all of it '
  'reached some of them -- so the unit cost will move once the '
  'missing figure is entered. why_provisional says which, in the same '
  'words the freight screen uses. A cost is not provisional merely '
  'for being recent: the flag means a known hole, not a guess.';


-- And the same for an item's average, which is what the stock screen
-- shows. An average over four receipts where one came in on a skewed
-- shipment is itself provisional -- less so than that one line, but
-- not a settled figure -- and it is the average, not the line, that
-- the margin and the price list are read off.
--
-- The count is carried as well as the flag, because "1 of 4 receipts"
-- and "4 of 4" deserve different confidence from a buyer, and a bare
-- true would flatten them.

create or replace view public.item_cost with (security_invoker = true) as
  with priced as (
    select m.org_id,
           m.item_id,
           m.qty,
           coalesce(lc.landed_unit_cost, m.unit_cost) as unit_cost,
           coalesce(lc.provisional, false)            as provisional,
           lc.why_provisional                         as why
      from public.stock_movement m
      left join public.landed_cost lc
        on m.source_type = 'grn_line'
       and lc.grn_line_id = m.source_id
       and lc.org_id = m.org_id
     where m.unit_cost is not null
       and (m.qty > 0 or m.reason = 'reversal')
  )
  select org_id, item_id,
         sum(qty * unit_cost) / nullif(sum(qty), 0) as avg_unit_cost,
         sum(qty)                                   as qty_received,
         bool_or(provisional)                       as provisional,
         count(*) filter (where provisional)        as provisional_receipts,
         count(*)                                   as receipts,
         -- The actual reasons, not a guess at them. An average can
         -- draw on two shipments that are unsure for different
         -- reasons, so this is the set and not the first of them.
         string_agg(distinct why, '; ' order by why)
           filter (where provisional)               as why_provisional
    from priced
   group by org_id, item_id;

comment on view public.item_cost is
  'Weighted average cost per item, preferring the landed cost where a '
  'movement came from a receipt line on a shipment. Reversals are '
  'included and carry the cost they withdraw, so a corrected receipt '
  'leaves the average on the corrected figure rather than halfway '
  'between the two. provisional is true when any receipt behind the '
  'average came in on a shipment with a charge that could not be '
  'divided properly; provisional_receipts of receipts says how much '
  'of the average rests on one, and why_provisional names the '
  'figures that are missing.';
