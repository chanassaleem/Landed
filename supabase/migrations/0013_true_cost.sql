-- The product computed the landed cost and then valued the stock at
-- the supplier's invoice price, in the supplier's currency.
--
-- A worked example from the test that found it. A hundred units at
-- EUR 50, bought at 310 to the euro, with PKR 620,000 of freight and
-- duty on top. The true landed cost is PKR 21,700 a unit. The app
-- showed PKR 50. Stock worth PKR 2,170,000 appeared on screen as
-- PKR 5,000 -- wrong by a factor of 434.
--
-- Three faults, each enough on its own:
--
--   The goods-receipt trigger posted `unit_price` into the ledger
--   with no exchange rate, so a euro price became a rupee cost.
--
--   `item_cost`, which every module reads through
--   engine/db/snapshot.py, averaged those movement costs. The
--   `landed_cost` view was computed, correct in shape, and read by
--   exactly one screen for display. Nothing valued anything with it.
--
--   `shipment_line_basis.fob_value` was quantity times unit price
--   with no rate, while the costs allocated against it were
--   converted. One sum, two currencies.
--
-- This is the claim the product is sold on -- that landed cost adds a
-- fifth to a half on top of FOB and a business pricing off the
-- supplier invoice is wrong by that much on every line. It was true
-- of this product.
--
-- The shape of the fix follows how the trade actually works. The
-- ledger records what happened: we received the goods and the
-- supplier invoiced this much, at this rate, on this day. All three
-- are facts at receipt. The landed cost is not a fact at receipt --
-- the clearing agent's invoice arrives weeks later -- so it is
-- derived in the valuation view, which means it corrects itself when
-- that invoice lands instead of needing history rewritten.

begin;

-- ------------------------------------------- what the receipt cost us

alter table public.grn_lines
  add column currency text,
  add column fx_rate numeric(18,6) not null default 1
    check (fx_rate > 0);

comment on column public.grn_lines.fx_rate is
  'Rate to the base currency on the day of receipt. The ledger stores '
  'unit_price * fx_rate, so a euro invoice becomes a rupee cost once, '
  'at a rate that is recorded rather than looked up later.';

-- Inherit the order's currency and rate unless the receipt states its
-- own. A receipt against a purchase order is in that order's currency
-- by definition; a direct receipt with no order is in base currency
-- until somebody says otherwise.
create or replace function app.grn_line_inherits_rate()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  po public.purchase_orders%rowtype;
begin
  if new.po_line_id is not null
     and (new.currency is null or new.fx_rate = 1) then
    select o.* into po
      from public.purchase_orders o
      join public.po_lines l on l.po_id = o.id
     where l.id = new.po_line_id;
    if found then
      new.currency := coalesce(new.currency, po.currency);
      if new.fx_rate = 1 then
        new.fx_rate := po.fx_rate;
      end if;
    end if;
  end if;
  return new;
end;
$$;

create trigger grn_lines_inherit_rate
  before insert on public.grn_lines
  for each row execute function app.grn_line_inherits_rate();

-- The ledger now holds the cost in base currency.
create or replace function app.grn_line_posts_movement()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  grn public.goods_receipts%rowtype;
begin
  select * into grn from public.goods_receipts where id = new.grn_id;

  insert into public.stock_movement
    (org_id, location_id, item_id, qty, unit_cost, reason,
     source_type, source_id, occurred_at, created_by)
  values
    (new.org_id, grn.location_id, new.item_id, new.qty,
     new.unit_price * new.fx_rate, 'grn',
     'grn_line', new.id, grn.received_at::timestamptz, grn.created_by);

  return new;
end;
$$;

-- ------------------------------------------------ correcting the past

-- Receipts already written inherit their order's rate.
update public.grn_lines g
   set currency = coalesce(g.currency, o.currency),
       fx_rate  = o.fx_rate
  from public.po_lines l
  join public.purchase_orders o on o.id = l.po_id
 where l.id = g.po_line_id and g.fx_rate = 1 and o.fx_rate <> 1;

-- And the movements they posted. The ledger is append-only and that
-- rule is right, but it protects facts: these rows hold a figure that
-- was never true in any currency, written by a trigger that was
-- wrong. Correcting them is not rewriting history, it is finishing
-- the write. A reversal pair would be the alternative and would leave
-- every affected item with two entries explaining a bug nobody but us
-- can see.
alter table public.stock_movement disable trigger stock_movement_no_update;

update public.stock_movement m
   set unit_cost = g.unit_price * g.fx_rate
  from public.grn_lines g
 where m.source_type = 'grn_line'
   and m.source_id = g.id
   and g.fx_rate <> 1
   and m.unit_cost is distinct from g.unit_price * g.fx_rate;

alter table public.stock_movement enable trigger stock_movement_no_update;

-- ------------------------------------------------- one currency, again

create or replace view public.shipment_line_basis
with (security_invoker = true) as
  select sl.org_id, sl.shipment_id, sl.grn_line_id,
         gl.item_id,
         gl.qty                                   as qty,
         gl.qty * gl.unit_price * gl.fx_rate      as fob_value,
         coalesce(sl.weight_kg, 0)                as weight_kg,
         coalesce(sl.volume_cbm, 0)               as volume_cbm
    from public.shipment_lines sl
    join public.grn_lines gl on gl.id = sl.grn_line_id;

-- --------------------------------------- valuation, at what it cost us

-- The figure every module reads. Landed where the shipment costs are
-- known, and the invoice price in base currency everywhere else --
-- opening stock, adjustments, and receipts whose clearing invoice has
-- not arrived.
--
-- Still a weighted average rather than FIFO. At this size nobody can
-- tell the difference in the figure and everybody can tell the
-- difference in the complexity.
create or replace view public.item_cost with (security_invoker = true) as
  with priced as (
    select m.org_id,
           m.item_id,
           m.qty,
           coalesce(lc.landed_unit_cost, m.unit_cost) as unit_cost
      from public.stock_movement m
      left join public.landed_cost lc
        on m.source_type = 'grn_line'
       and lc.grn_line_id = m.source_id
       and lc.org_id = m.org_id
     where m.qty > 0
       and m.unit_cost is not null
  )
  select org_id, item_id,
         sum(qty * unit_cost) / nullif(sum(qty), 0) as avg_unit_cost,
         sum(qty)                                   as qty_received
    from priced
   group by org_id, item_id;

commit;
