-- Buying, receiving, and what the goods actually cost once they arrive.
--
-- Three states that disagree constantly in a real importer, and the
-- disagreements are the point: ordered, received, invoiced. A short
-- shipment, a damaged carton, a supplier invoice that turns up three
-- weeks later at a different price — a system that models these as one
-- event cannot tell you that you were overcharged.

create table public.purchase_orders (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  location_id uuid not null references public.locations (id),
  supplier_id uuid references public.suppliers (id),
  po_number   text not null,
  currency    text not null default 'PKR' check (length(currency) = 3),
  -- Rate to the org's own currency at the time of ordering. Held per
  -- document because it moves, and an importer's margin lives in it.
  fx_rate     numeric(18,6) not null default 1 check (fx_rate > 0),
  status      text not null default 'draft'
              check (status in ('draft','placed','part_received','received','cancelled')),
  ordered_at  date,
  expected_at date,
  note        text,
  created_at  timestamptz not null default now(),
  created_by  uuid references auth.users (id),
  unique (org_id, po_number)
);

create table public.po_lines (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  po_id       uuid not null references public.purchase_orders (id) on delete cascade,
  item_id     uuid not null references public.items (id),
  qty         numeric(18,4) not null check (qty > 0),
  unit_price  numeric(18,4) not null check (unit_price >= 0),
  created_at  timestamptz not null default now()
);

create index po_lines_po_idx on public.po_lines (po_id);

create table public.goods_receipts (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  location_id uuid not null references public.locations (id),
  -- Nullable: stock does arrive without a purchase order, and refusing to
  -- record that just means it gets recorded nowhere.
  po_id       uuid references public.purchase_orders (id),
  grn_number  text not null,
  received_at date not null default current_date,
  note        text,
  created_at  timestamptz not null default now(),
  created_by  uuid references auth.users (id),
  unique (org_id, grn_number)
);

create table public.grn_lines (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  grn_id      uuid not null references public.goods_receipts (id) on delete cascade,
  po_line_id  uuid references public.po_lines (id),
  item_id     uuid not null references public.items (id),
  qty         numeric(18,4) not null check (qty > 0),
  -- What the supplier charged for the goods themselves, before freight.
  unit_price  numeric(18,4) not null default 0 check (unit_price >= 0),
  expiry_date date,
  batch       text,
  created_at  timestamptz not null default now()
);

create index grn_lines_grn_idx on public.grn_lines (grn_id);

-- Receiving goods IS a stock movement; this is where the document becomes
-- a fact. Done in the database rather than the API so that an import, a
-- phone and a script cannot each forget it in their own way.
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
    (new.org_id, grn.location_id, new.item_id, new.qty, new.unit_price, 'grn',
     'grn_line', new.id, grn.received_at::timestamptz, grn.created_by);

  return new;
end;
$$;

create trigger grn_lines_post
  after insert on public.grn_lines
  for each row execute function app.grn_line_posts_movement();

-- ---------------------------------------------------------------- freight
--
-- The part almost nobody at this price point does properly. An imported
-- item's real cost is not the supplier's price: it is that plus freight,
-- duty, clearing, inland transport and bank charges, divided across the
-- shipment. Get it wrong and every figure downstream is wrong with it,
-- because unit cost feeds consumption value, which feeds the ABC class,
-- which feeds the service target, which feeds safety stock.

create table public.shipments (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  reference   text not null,
  supplier_id uuid references public.suppliers (id),
  mode        text check (mode in ('sea','air','road','rail','courier')),
  etd         date,
  eta         date,
  arrived_at  date,
  status      text not null default 'in_transit'
              check (status in ('in_transit','arrived','cleared','closed')),
  created_at  timestamptz not null default now(),
  unique (org_id, reference)
);

-- allocate_by is the only interesting decision on this table. Duty follows
-- value, ocean freight follows volume, air freight follows weight, and a
-- flat clearing fee follows unit count. Choosing one basis for everything
-- is the usual shortcut and it is how a heavy cheap line ends up carrying
-- the duty of a light expensive one.
create table public.shipment_costs (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  shipment_id uuid not null references public.shipments (id) on delete cascade,
  kind        text not null check (kind in
                ('freight','duty','clearing','inland','insurance','bank','other')),
  amount      numeric(18,4) not null check (amount >= 0),
  currency    text not null default 'PKR' check (length(currency) = 3),
  fx_rate     numeric(18,6) not null default 1 check (fx_rate > 0),
  allocate_by text not null default 'value'
              check (allocate_by in ('value','weight','volume','qty')),
  note        text,
  created_at  timestamptz not null default now()
);

create table public.shipment_lines (
  shipment_id uuid not null references public.shipments (id) on delete cascade,
  grn_line_id uuid not null references public.grn_lines (id) on delete cascade,
  org_id      uuid not null references public.organisations (id) on delete cascade,
  weight_kg   numeric(18,4),
  volume_cbm  numeric(18,6),
  primary key (shipment_id, grn_line_id)
);

-- ------------------------------------------------------- landed cost views

create view public.shipment_line_basis with (security_invoker = true) as
  select sl.org_id, sl.shipment_id, sl.grn_line_id,
         gl.item_id,
         gl.qty                                   as qty,
         gl.qty * gl.unit_price                   as fob_value,
         coalesce(sl.weight_kg, 0)                as weight_kg,
         coalesce(sl.volume_cbm, 0)               as volume_cbm
    from public.shipment_lines sl
    join public.grn_lines gl on gl.id = sl.grn_line_id;

create view public.shipment_totals with (security_invoker = true) as
  select org_id, shipment_id,
         sum(fob_value)  as fob_value,
         sum(weight_kg)  as weight_kg,
         sum(volume_cbm) as volume_cbm,
         sum(qty)        as qty
    from public.shipment_line_basis
   group by org_id, shipment_id;

-- Each line's share of every cost on its shipment, on that cost's own
-- basis, plus the per-unit figure the engine should be using as unit cost.
create view public.landed_cost with (security_invoker = true) as
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
         ), 0)) / nullif(b.qty, 0) as landed_unit_cost
    from public.shipment_line_basis b
    join public.shipment_totals t
      on t.shipment_id = b.shipment_id and t.org_id = b.org_id
    left join public.shipment_costs c
      on c.shipment_id = b.shipment_id
   group by b.org_id, b.grn_line_id, b.item_id, b.qty, b.fob_value;

-- -------------------------------------------------------------------- RLS

-- NOTE: never `for all` on a write policy. A FOR ALL policy's USING
-- clause also governs SELECT, and permissive policies are OR'd together,
-- so one `for all using (has_role(...))` silently re-opens read access to
-- anyone with the role -- bypassing both the module gate and the branch
-- scoping in the read policy above it. Split by command, every time.
alter table public.purchase_orders enable row level security;
alter table public.po_lines        enable row level security;
alter table public.goods_receipts  enable row level security;
alter table public.grn_lines       enable row level security;
alter table public.shipments       enable row level security;
alter table public.shipment_costs  enable row level security;
alter table public.shipment_lines  enable row level security;

create policy po_read on public.purchase_orders for select
  using (org_id in (select app.org_ids())
         and (select app.can_see_location(org_id, location_id))
         and (select app.has_module(org_id, 'buy')));
create policy po_insert on public.purchase_orders for insert
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy po_update on public.purchase_orders for update
  using ((select app.has_role(org_id, array['owner','manager'])))
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy po_delete on public.purchase_orders for delete
  using ((select app.has_role(org_id, array['owner','manager'])));

create policy pol_read on public.po_lines for select
  using (org_id in (select app.org_ids()) and (select app.has_module(org_id, 'buy')));
create policy pol_insert on public.po_lines for insert
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy pol_update on public.po_lines for update
  using ((select app.has_role(org_id, array['owner','manager'])))
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy pol_delete on public.po_lines for delete
  using ((select app.has_role(org_id, array['owner','manager'])));

create policy grn_read on public.goods_receipts for select
  using (org_id in (select app.org_ids())
         and (select app.can_see_location(org_id, location_id))
         and (select app.has_module(org_id, 'buy')));
create policy grn_insert on public.goods_receipts for insert
  with check ((select app.has_role(org_id, array['owner','manager','staff'])));
create policy grn_update on public.goods_receipts for update
  using ((select app.has_role(org_id, array['owner','manager','staff'])))
  with check ((select app.has_role(org_id, array['owner','manager','staff'])));
create policy grn_delete on public.goods_receipts for delete
  using ((select app.has_role(org_id, array['owner','manager','staff'])));

create policy grnl_read on public.grn_lines for select
  using (org_id in (select app.org_ids()) and (select app.has_module(org_id, 'buy')));
create policy grnl_insert on public.grn_lines for insert
  with check ((select app.has_role(org_id, array['owner','manager','staff'])));
create policy grnl_update on public.grn_lines for update
  using ((select app.has_role(org_id, array['owner','manager','staff'])))
  with check ((select app.has_role(org_id, array['owner','manager','staff'])));
create policy grnl_delete on public.grn_lines for delete
  using ((select app.has_role(org_id, array['owner','manager','staff'])));

create policy ship_read on public.shipments for select
  using (org_id in (select app.org_ids()) and (select app.has_module(org_id, 'freight')));
create policy ship_insert on public.shipments for insert
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy ship_update on public.shipments for update
  using ((select app.has_role(org_id, array['owner','manager'])))
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy ship_delete on public.shipments for delete
  using ((select app.has_role(org_id, array['owner','manager'])));

create policy shipc_read on public.shipment_costs for select
  using (org_id in (select app.org_ids()) and (select app.has_module(org_id, 'freight')));
create policy shipc_insert on public.shipment_costs for insert
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy shipc_update on public.shipment_costs for update
  using ((select app.has_role(org_id, array['owner','manager'])))
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy shipc_delete on public.shipment_costs for delete
  using ((select app.has_role(org_id, array['owner','manager'])));

create policy shipl_read on public.shipment_lines for select
  using (org_id in (select app.org_ids()) and (select app.has_module(org_id, 'freight')));
create policy shipl_insert on public.shipment_lines for insert
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy shipl_update on public.shipment_lines for update
  using ((select app.has_role(org_id, array['owner','manager'])))
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy shipl_delete on public.shipment_lines for delete
  using ((select app.has_role(org_id, array['owner','manager'])));
