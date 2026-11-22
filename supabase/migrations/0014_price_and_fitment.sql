-- What a line sells for, and what it is used on.
--
-- Two columns and a link table, and both earn their place now that
-- migration 0013 made the cost figure true.
--
-- A selling price next to a landed cost is a margin per line. Before
-- 0013 that would have been a lie dressed as a number -- margin
-- against an invoice price in the wrong currency -- which is why this
-- waits until now.
--
-- Fitment is the other half, and it is the question a diagnostics
-- distributor actually asks about a reagent: which analyser consumes
-- it. It decides whether a slow-moving line is dead stock or the only
-- consumable for a machine placed last month, and no quantity on a
-- shelf can tell you which.
--
-- A link table rather than a column on the item, because a reagent
-- commonly fits several analysers and a column would force somebody
-- to pick one and be wrong. `is_generic` is the other case, said
-- plainly: a box of gloves fits everything, and an empty link list
-- would otherwise read as "nobody has filled this in yet".

begin;

alter table public.items
  add column list_price numeric(18,2) check (list_price >= 0),
  add column is_generic boolean not null default false;

comment on column public.items.list_price is
  'What you sell it for, in base currency. Against the landed cost in '
  'public.item_cost this is the margin on the line -- which is only '
  'meaningful because that cost is now the landed one.';

comment on column public.items.is_generic is
  'Fits anything, so an empty fitment list is a fact rather than a '
  'blank somebody still has to fill in.';

create table public.item_machines (
  org_id     uuid not null references public.organisations (id) on delete cascade,
  item_id    uuid not null references public.items (id) on delete cascade,
  machine_id uuid not null references public.machines (id) on delete cascade,
  created_at timestamptz not null default clock_timestamp(),
  primary key (item_id, machine_id)
);

create index item_machines_machine_idx
  on public.item_machines (org_id, machine_id);

alter table public.item_machines enable row level security;

create policy im_read on public.item_machines for select
  using (org_id in (select app.org_ids()));
create policy im_write on public.item_machines for insert
  with check ((select app.has_role(org_id, array['owner','manager','staff'])));
create policy im_delete on public.item_machines for delete
  using ((select app.has_role(org_id, array['owner','manager','staff'])));

commit;
