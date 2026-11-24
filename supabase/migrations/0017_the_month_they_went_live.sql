-- The month they went live belongs to whoever has the whole of it.
--
-- 0015 fixed a double count by giving the ledger any month it has
-- movements for. That is right for every month but one: the month the
-- branch started recording sales. The ledger covers part of it -- from
-- the day they began -- and the imported history covers all of it.
-- 0015 took the ledger's part and dropped the history's whole.
--
-- Reproduced on a clean database. A branch goes live on 20 March.
-- History: February 100, March 100. Ledger: 40 sold from 20 March,
-- 105 in April. demand_monthly returned:
--
--   2026-02    100   history
--   2026-03     40   ledger      <- eleven days of a 31-day month
--   2026-04    105   ledger
--
-- A forecast reading 100, 40, 105 does not see one short month. It
-- sees a collapse and a recovery, and that is the expensive part: the
-- swing inflates the coefficient of variation, which is what decides
-- whether Syntetos-Boylan calls the item smooth or erratic, which
-- decides whether it is forecast by Holt or by Croston, and which
-- feeds the safety stock. One partial month at the seam moves the
-- order quantity on an item that never changed how it sells.
--
-- The rule: a branch's ledger opens in the month of its first recorded
-- sale. After that month the ledger is the only truth, silence
-- included -- a month with no sales is a month that sold nothing, not
-- a month to fill from an old import. Before that month, history. The
-- opening month itself goes to history where history has it, because
-- history has the whole month and the ledger has a fragment of it.
--
-- Per branch rather than per item, because going live is something a
-- branch does, not something each item does separately. Taken per
-- item, a slow mover whose first sale came three weeks after go-live
-- would have had its own later cutover, and the quiet weeks before it
-- -- which really did sell nothing -- would have been overwritten by
-- an imported figure.
--
-- Where the ledger's opening month is not in the history, the
-- fragment is all there is. It is kept rather than dropped, and named
-- 'ledger (part month)' so that a short first month is visible as a
-- short first month instead of passing for a bad one.

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
  ),
  -- The month this branch began recording sales. min() over the
  -- branch, so one busy item establishes the date for all of them.
  opened as (
    select org_id, location_id, min(period) as period
      from ledger
     group by org_id, location_id
  )
  -- Every month after the branch opened: the ledger, and nothing else.
  select l.org_id, l.location_id, l.item_id, l.period, l.quantity,
         'ledger'::text as source
    from ledger l
    join opened o on o.org_id = l.org_id
                 and o.location_id = l.location_id
   where l.period > o.period
  union all
  -- The opening month, where history does not have it. A fragment,
  -- said to be one.
  select l.org_id, l.location_id, l.item_id, l.period, l.quantity,
         'ledger (part month)'::text as source
    from ledger l
    join opened o on o.org_id = l.org_id
                 and o.location_id = l.location_id
   where l.period = o.period
     and not exists (
       select 1 from public.demand_history h
        where h.org_id      = l.org_id
          and h.location_id = l.location_id
          and h.item_id     = l.item_id
          and h.period      = l.period
     )
  union all
  -- History, up to and including the opening month. A branch with no
  -- recorded sales yet has no opening month, and keeps all of it.
  select h.org_id, h.location_id, h.item_id, h.period, h.quantity,
         h.source
    from public.demand_history h
    left join opened o on o.org_id = h.org_id
                      and o.location_id = h.location_id
   where o.period is null
      or h.period <= o.period;

comment on view public.demand_monthly is
  'Demand per item per branch per month, each month from exactly one '
  'source. A branch opens in the month of its first recorded sale: '
  'after it the ledger is the only truth, including the months it is '
  'silent about; before it, imported history. The opening month goes '
  'to history where history has it, because the ledger holds only the '
  'part of that month after they started. Summing the two doubled the '
  'forecast; taking the ledger fragment made a short month look like '
  'a collapse.';
