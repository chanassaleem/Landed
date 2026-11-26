-- Correcting a receipt without rewriting what happened.
--
-- A receipt is an event. The stock went on the shelf, the valuation
-- took the cost, and a month of figures was built on both. When the
-- warehouse says the pallet held ninety rather than a hundred, the
-- answer is not to edit the hundred away: somebody reconciled against
-- that hundred, and a figure that changes under them with nothing to
-- show for it is worse than a figure that was wrong.
--
-- So a correction is an entry of its own. The original line stays
-- exactly as it was filed. A correction line carries the right
-- figures and points at the line it replaces, and the ledger gets a
-- reversal of the original movement alongside the corrected one. Three
-- rows, netting to ninety, and all three legible: what was filed, that
-- it was withdrawn, and what replaced it.
--
-- The hard part is not the entry. It is that a superseded line must
-- stop counting everywhere at once -- the outstanding quantity on the
-- order, the basis freight is divided by, the valuation -- because a
-- correction that reaches one of those and not the others is worse
-- than no correction at all. One place to say what still counts, and
-- the three readers all read it.

alter table public.grn_lines
  -- The line this one replaces. Null on an ordinary receipt.
  add column supersedes uuid references public.grn_lines (id),
  -- Why, in the words of whoever corrected it. A correction without a
  -- reason is an unexplained change to a historical figure, which is
  -- the thing this exists to avoid.
  add column note text,
  add column created_by uuid references auth.users (id)
                        default auth.uid(),
  add constraint grn_lines_no_self_correction
      check (supersedes is distinct from id);

-- A line is corrected once. Twice and the chain forks, and there is no
-- longer a single answer to what the figure is now. A correction can
-- itself be corrected -- that is a chain, not a fork.
create unique index grn_lines_one_correction_each
  on public.grn_lines (supersedes) where supersedes is not null;

comment on column public.grn_lines.supersedes is
  'The receipt line this one corrects. The superseded line keeps its '
  'original figures and stops counting; both stay readable, so the '
  'history shows what was filed as well as what replaced it.';

-- What still counts. The single place the three readers agree on.
create view public.grn_lines_live with (security_invoker = true) as
  select gl.*
    from public.grn_lines gl
   where not exists (
     select 1 from public.grn_lines c
      where c.supersedes = gl.id
   );

comment on view public.grn_lines_live is
  'Receipt lines that have not been superseded by a correction. Every '
  'reader that sums received quantity or divides a charge uses this, '
  'because a correction visible to one of them and not the others is '
  'worse than no correction at all.';

-- Freight is divided across the lines that are really there.
create or replace view public.shipment_line_basis
with (security_invoker = true) as
  select sl.org_id, sl.shipment_id, sl.grn_line_id,
         gl.item_id,
         gl.qty                                            as qty,
         gl.qty * gl.unit_price * gl.fx_rate               as fob_value,
         coalesce(sl.weight_kg, 0)                         as weight_kg,
         coalesce(sl.volume_cbm, 0)                        as volume_cbm
    from public.shipment_lines sl
    join public.grn_lines_live gl on gl.id = sl.grn_line_id;

-- What a branch has on order and not yet received, with corrections
-- counted. Named here rather than written out in three places.
create view public.po_outstanding with (security_invoker = true) as
  with received as (
    select org_id, po_line_id, sum(qty) as qty
      from public.grn_lines_live
     where po_line_id is not null
     group by org_id, po_line_id
  )
  select pl.org_id,
         pl.po_id,
         pl.id          as po_line_id,
         po.po_number,
         po.location_id,
         pl.item_id,
         pl.qty         as ordered,
         coalesce(r.qty, 0)              as received,
         pl.qty - coalesce(r.qty, 0)     as outstanding,
         po.currency,
         po.fx_rate,
         po.status,
         po.expected_at
    from public.po_lines pl
    join public.purchase_orders po on po.id = pl.po_id
    left join received r on r.po_line_id = pl.id
                        and r.org_id = pl.org_id;

comment on view public.po_outstanding is
  'Ordered, received and still outstanding per order line, with '
  'superseded receipt lines excluded. A receipt that filed no line '
  'never reduced the outstanding quantity, so the Ordering screen '
  'recommended stock that was already in the warehouse.';

-- The valuation has to see a reversal, or a correction never lands.
--
-- item_cost averaged every movement with a cost and a positive
-- quantity. A reversal is negative by construction, so it was filtered
-- out: correct a receipt from 17,080 to 18,000 a unit and the average
-- read 17,540 -- the midpoint of the wrong figure and the right one,
-- for as long as the stock existed. Found by correcting a price and
-- asking what the stock was then worth.
--
-- Including reversals is not a special case, it is the arithmetic
-- working as intended: a weighted average over +100 at 17,080, -100 at
-- 17,080 and +100 at 18,000 is 18,000. The row that withdraws carries
-- the same cost as the row it withdraws, so the two cancel exactly.
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
     where m.unit_cost is not null
       and (m.qty > 0 or m.reason = 'reversal')
  )
  select org_id, item_id,
         sum(qty * unit_cost) / nullif(sum(qty), 0) as avg_unit_cost,
         sum(qty)                                   as qty_received
    from priced
   group by org_id, item_id;

comment on view public.item_cost is
  'Weighted average cost per item, preferring the landed cost where a '
  'movement came from a receipt line on a shipment. Reversals are '
  'included and carry the cost they withdraw, so a corrected receipt '
  'leaves the average on the corrected figure rather than halfway '
  'between the two.';
