-- The movement ledger. This is the system of record.
--
-- Everything that changes stock writes one row here: a sale, a goods
-- receipt, a transfer, a count adjustment, an opening balance. Stock on
-- hand is not stored anywhere — it is the sum of these rows. That choice
-- costs a little speed and buys the one property that matters: there is no
-- second number that can disagree with the ledger, because there is no
-- second number.
--
-- The table is append-only, enforced by trigger rather than by policy, so
-- that it holds even against the service role. A mistake is corrected with
-- a reversing entry, not by editing history — which is also what makes the
-- ledger safe to sync from a phone that was offline: appends from two
-- devices never conflict, they just both append.

create table public.stock_movement (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  location_id uuid not null references public.locations (id),
  item_id     uuid not null references public.items (id),

  -- Signed. Positive puts stock on the shelf, negative takes it off.
  qty         numeric(18,4) not null check (qty <> 0),

  -- Cost per unit for this movement, in the org's currency. Set on
  -- receipts; left null on issues, which are valued from the running
  -- average rather than carrying a price of their own.
  unit_cost   numeric(18,4) check (unit_cost is null or unit_cost >= 0),

  reason      text not null check (reason in (
                'opening',      -- the balance when the org started here
                'grn',          -- goods received against a purchase order
                'sale',         -- sold to a customer
                'issue',        -- consumed or dispensed, not sold
                'return_in',    -- customer returned it
                'return_out',   -- returned to the supplier
                'transfer_in',
                'transfer_out',
                'adjustment',   -- a stock count disagreed with the ledger
                'write_off',    -- damaged, expired, lost
                'reversal')),   -- undoes an earlier movement

  -- What caused it. source_id points at the GRN line, the sale, or the
  -- movement being reversed.
  source_type text check (source_type in
                ('grn_line','sale','transfer','count','manual','import','movement')),
  source_id   uuid,

  -- When it happened in the real world, which is not when it was recorded.
  -- A phone that was offline for an hour records the true time.
  occurred_at timestamptz not null default now(),
  created_at  timestamptz not null default now(),
  created_by  uuid references auth.users (id),

  -- Idempotency for offline sync: the client generates this, so replaying
  -- a queued write after a flaky reconnect cannot double-count.
  client_ref  text,

  note        text,
  custom      jsonb not null default '{}'::jsonb
);

create unique index stock_movement_client_ref_idx
  on public.stock_movement (org_id, client_ref) where client_ref is not null;

-- The engine's hot path: every movement for one item at one branch, in order.
create index stock_movement_position_idx
  on public.stock_movement (org_id, location_id, item_id, occurred_at);

-- Demand history, which reads by period rather than by item.
create index stock_movement_demand_idx
  on public.stock_movement (org_id, occurred_at)
  where reason in ('sale','issue');

-- ------------------------------------------------------------ append-only

create or replace function app.ledger_is_append_only()
returns trigger
language plpgsql
as $$
begin
  raise exception
    'stock_movement is append-only: % is not allowed. Post a reversal '
    '(reason = ''reversal'', source_type = ''movement'', source_id = the '
    'movement being undone) instead of changing history.', tg_op;
end;
$$;

create trigger stock_movement_no_update
  before update or delete on public.stock_movement
  for each row execute function app.ledger_is_append_only();

-- --------------------------------------------- history from before Landed
--
-- An importer onboarding today has a year of demand that never passed
-- through this system. It is kept apart from the ledger deliberately:
-- one table is what we observed, the other is what we were told, and a
-- figure built on the second should be able to say so.

create table public.demand_history (
  org_id      uuid not null references public.organisations (id) on delete cascade,
  location_id uuid not null references public.locations (id) on delete cascade,
  item_id     uuid not null references public.items (id) on delete cascade,
  period      text not null check (period ~ '^\d{4}-\d{2}$'),
  quantity    numeric(18,4) not null check (quantity >= 0),
  source      text not null default 'import',
  created_at  timestamptz not null default now(),
  primary key (org_id, location_id, item_id, period)
);

-- ------------------------------------------------------------------ views
--
-- security_invoker is not optional. Without it a view runs with its
-- OWNER's rights and quietly serves every tenant's rows to any caller —
-- the single most common way an otherwise careful Supabase schema leaks.

create view public.stock_on_hand with (security_invoker = true) as
  select org_id, location_id, item_id,
         sum(qty)                                        as on_hand,
         sum(qty) filter (where reason = 'grn')          as received_ever,
         max(occurred_at)                                as last_movement_at
    from public.stock_movement
   group by org_id, location_id, item_id;

-- Average cost of what was received, which is the moving average a small
-- importer actually wants. It is not FIFO and does not pretend to be:
-- FIFO needs cost layers and a consumption order, and at this size nobody
-- can tell the difference in the figure but everybody can tell the
-- difference in the complexity.
create view public.item_cost with (security_invoker = true) as
  select org_id, item_id,
         sum(qty * unit_cost) / nullif(sum(qty), 0) as avg_unit_cost,
         sum(qty)                                   as qty_received
    from public.stock_movement
   where qty > 0 and unit_cost is not null
   group by org_id, item_id;

-- Monthly demand, from the ledger where we have it and from the imported
-- history where we do not. A month with no row is a month with no demand,
-- and the snapshot builder fills those zeros before the engine sees them,
-- because to a forecast a zero month is information and a gap is not.
create view public.demand_monthly with (security_invoker = true) as
  select org_id, location_id, item_id,
         to_char(occurred_at, 'YYYY-MM') as period,
         sum(-qty)                       as quantity,
         'ledger'                        as source
    from public.stock_movement
   where reason in ('sale','issue')
   group by org_id, location_id, item_id, to_char(occurred_at, 'YYYY-MM')
  union all
  select org_id, location_id, item_id, period, quantity, source
    from public.demand_history;

-- -------------------------------------------------------------------- RLS

-- NOTE: never `for all` on a write policy. A FOR ALL policy's USING
-- clause also governs SELECT, and permissive policies are OR'd together,
-- so one `for all using (has_role(...))` silently re-opens read access to
-- anyone with the role -- bypassing both the module gate and the branch
-- scoping in the read policy above it. Split by command, every time.
alter table public.stock_movement  enable row level security;
alter table public.demand_history  enable row level security;

create policy mv_read on public.stock_movement for select
  using (org_id in (select app.org_ids())
         and (select app.can_see_location(org_id, location_id))
         and (select app.has_module(org_id, 'stock')));

-- Staff record movements; nobody edits or deletes them. The absence of an
-- update or delete policy is deliberate and the trigger above backs it up.
create policy mv_insert on public.stock_movement for insert
  with check ((select app.has_role(org_id, array['owner','manager','staff']))
              and (select app.can_see_location(org_id, location_id))
              and (select app.has_module(org_id, 'stock')));

create policy dh_read on public.demand_history for select
  using (org_id in (select app.org_ids())
         and (select app.can_see_location(org_id, location_id)));

create policy dh_insert on public.demand_history for insert
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy dh_update on public.demand_history for update
  using ((select app.has_role(org_id, array['owner','manager'])))
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy dh_delete on public.demand_history for delete
  using ((select app.has_role(org_id, array['owner','manager'])));
