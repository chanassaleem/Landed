-- One month's demand, counted once.
--
-- `demand_monthly` was a UNION ALL of two sources: sales derived from
-- the ledger, and the Sales history sheet. A business that imports its
-- history and then records sales in the app has both for the same
-- month, and the view returned both. The engine summed them.
--
-- Reproduced on a clean database: import 100 for this month, record a
-- sale of 100 for this month, and the forecast reads 200. It is not a
-- display fault. Demand feeds the forecast, the forecast feeds the
-- reorder point and the safety stock, and the reorder point feeds the
-- order quantity on the Ordering screen -- so an importer is told to
-- buy twice what they need, in a trade where that is months of cash in
-- a warehouse.
--
-- The rule: where the ledger has movements for an item at a branch in
-- a month, the ledger is the truth for that month. Imported history
-- is a backfill for the period before they started recording sales,
-- and it fills only the months the ledger does not cover.
--
-- Not "the newest wins" and not a sum. A month is either one they
-- recorded or one they imported, and mixing the two within a month
-- would mean a figure nobody can reconcile against either source.

create or replace view public.demand_monthly
with (security_invoker = true) as
  with ledger as (
    select org_id, location_id, item_id,
           to_char(occurred_at, 'YYYY-MM') as period,
           sum(-qty)                       as quantity
      from public.stock_movement
     where reason in ('sale', 'issue')
     group by org_id, location_id, item_id,
              to_char(occurred_at, 'YYYY-MM')
  )
  select org_id, location_id, item_id, period, quantity,
         'ledger'::text as source
    from ledger
  union all
  select h.org_id, h.location_id, h.item_id, h.period, h.quantity,
         h.source
    from public.demand_history h
   where not exists (
     select 1 from ledger l
      where l.org_id      = h.org_id
        and l.location_id = h.location_id
        and l.item_id     = h.item_id
        and l.period      = h.period
   );

comment on view public.demand_monthly is
  'Demand per item per branch per month, each month from one source. '
  'The ledger wins where it has movements; imported history fills the '
  'months before the business started recording sales. Summing the two '
  'doubled the forecast and the reorder point behind it.';
