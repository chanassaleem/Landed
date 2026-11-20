-- What the business sells and who it buys from.
--
-- Note what is NOT here: on_hand, and the current unit cost. Both are
-- derived from the ledger in 0003 and stored nowhere, because a stored
-- quantity and a ledger that disagree is the oldest bug in inventory
-- software and there is no way to tell which one is lying.

create table public.suppliers (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  code        text not null,
  name        text not null,
  country     text,
  currency    text check (length(currency) = 3),
  lead_time_days numeric(8,2),
  custom      jsonb not null default '{}'::jsonb,
  is_active   boolean not null default true,
  created_at  timestamptz not null default now(),
  unique (org_id, code)
);

create table public.items (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  sku         text not null,
  description text not null,
  category    text not null default '',
  uom         text not null default 'each',
  barcode     text,
  supplier_id uuid references public.suppliers (id) on delete set null,
  -- Policy inputs the engine reads. Per-branch overrides live below.
  lead_time_days      numeric(8,2) not null default 0,
  lead_time_days_std  numeric(8,2) not null default 0,
  is_clinical_critical boolean not null default false,
  -- Fields this organisation invented. Never read by a calculation: the
  -- engine reads typed columns only, so that every figure it produces can
  -- be reproduced from a known schema.
  custom      jsonb not null default '{}'::jsonb,
  is_active   boolean not null default true,
  created_at  timestamptz not null default now(),
  unique (org_id, sku)
);

create index items_barcode_idx on public.items (org_id, barcode) where barcode is not null;
create index items_custom_idx  on public.items using gin (custom);

-- Per-branch overrides. A row here only where the branch differs from the
-- item's default; a null column means "use the item's value".
create table public.item_locations (
  org_id      uuid not null references public.organisations (id) on delete cascade,
  item_id     uuid not null references public.items (id) on delete cascade,
  location_id uuid not null references public.locations (id) on delete cascade,
  lead_time_days      numeric(8,2),
  lead_time_days_std  numeric(8,2),
  expiry_date date,
  created_at  timestamptz not null default now(),
  primary key (item_id, location_id)
);

-- The per-organisation definition of the extra columns above. These drive
-- the form, the grid column, the validation and the import template; the
-- values live in the `custom` JSONB of the entity they belong to.
create table public.field_definitions (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  entity      text not null check (entity in ('item','supplier','movement')),
  key         text not null check (key ~ '^[a-z][a-z0-9_]*$'),
  label       text not null,
  type        text not null check (type in ('text','number','date','select','boolean')),
  options     text[] not null default '{}',
  required    boolean not null default false,
  position    integer not null default 0,
  created_at  timestamptz not null default now(),
  unique (org_id, entity, key)
);

-- NOTE: never `for all` on a write policy. A FOR ALL policy's USING
-- clause also governs SELECT, and permissive policies are OR'd together,
-- so one `for all using (has_role(...))` silently re-opens read access to
-- anyone with the role -- bypassing both the module gate and the branch
-- scoping in the read policy above it. Split by command, every time.
alter table public.suppliers         enable row level security;
alter table public.items             enable row level security;
alter table public.item_locations    enable row level security;
alter table public.field_definitions enable row level security;

create policy sup_read on public.suppliers for select
  using (org_id in (select app.org_ids()) and (select app.has_module(org_id, 'buy')));
create policy sup_insert on public.suppliers for insert
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy sup_update on public.suppliers for update
  using ((select app.has_role(org_id, array['owner','manager'])))
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy sup_delete on public.suppliers for delete
  using ((select app.has_role(org_id, array['owner','manager'])));

create policy item_read on public.items for select
  using (org_id in (select app.org_ids()) and (select app.has_module(org_id, 'stock')));
create policy item_insert on public.items for insert
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy item_update on public.items for update
  using ((select app.has_role(org_id, array['owner','manager'])))
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy item_delete on public.items for delete
  using ((select app.has_role(org_id, array['owner','manager'])));

create policy itemloc_read on public.item_locations for select
  using (org_id in (select app.org_ids())
         and (select app.can_see_location(org_id, location_id)));
create policy itemloc_insert on public.item_locations for insert
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy itemloc_update on public.item_locations for update
  using ((select app.has_role(org_id, array['owner','manager'])))
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy itemloc_delete on public.item_locations for delete
  using ((select app.has_role(org_id, array['owner','manager'])));

create policy fielddef_read on public.field_definitions for select
  using (org_id in (select app.org_ids()));
create policy fielddef_insert on public.field_definitions for insert
  with check ((select app.has_role(org_id, array['owner'])));
create policy fielddef_update on public.field_definitions for update
  using ((select app.has_role(org_id, array['owner'])))
  with check ((select app.has_role(org_id, array['owner'])));
create policy fielddef_delete on public.field_definitions for delete
  using ((select app.has_role(org_id, array['owner'])));
