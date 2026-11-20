-- The spine: who the data belongs to, who may see it, and what they pay for.
--
-- Three rules hold everywhere below and are worth stating once:
--
--   1. Every tenant-owned table carries org_id, and every policy begins with
--      the same wall: the row's org must be one the caller belongs to.
--   2. The helper functions live in the `app` schema, NOT in `public`.
--      PostgREST exposes `public`, and a SECURITY DEFINER function in an
--      exposed schema can be called straight through the API, which would
--      hand a caller the very bypass the functions exist to contain.
--   3. Tenancy is never read from user_metadata. That field is writable by
--      the user, so a tenant id taken from it is not a boundary at all.

create extension if not exists pgcrypto;

create schema if not exists app;
revoke all on schema app from public, anon, authenticated;

-- ---------------------------------------------------------------- tenants

create table public.organisations (
  id          uuid primary key default gen_random_uuid(),
  name        text not null check (length(trim(name)) > 0),
  country     text not null default 'PK',
  currency    text not null default 'PKR' check (length(currency) = 3),
  -- The as-of date and policy thresholds the engine reads. Held as JSONB so
  -- a new threshold does not need a migration; the engine validates them.
  settings    jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now(),
  created_by  uuid references auth.users (id)
);

create table public.locations (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  code        text not null,
  name        text not null,
  city        text,
  is_warehouse boolean not null default true,
  is_active   boolean not null default true,
  created_at  timestamptz not null default now(),
  unique (org_id, code)
);

-- role:
--   owner      — everything in the org, including billing and members
--   manager    — everything at the locations they are scoped to
--   staff      — records movements at their locations; cannot change policy
--   accountant — reads everything, writes nothing. May span organisations,
--                which is how a bookkeeper serves several clients without
--                a second tenancy concept.
create table public.memberships (
  org_id       uuid not null references public.organisations (id) on delete cascade,
  user_id      uuid not null references auth.users (id) on delete cascade,
  role         text not null check (role in ('owner','manager','staff','accountant')),
  -- Empty means every location in the org. One column covers both the
  -- single-shop owner and the multi-branch manager.
  location_ids uuid[] not null default '{}',
  status       text not null default 'active' check (status in ('active','suspended')),
  invited_by   uuid references auth.users (id),
  created_at   timestamptz not null default now(),
  primary key (org_id, user_id)
);

create index memberships_user_idx on public.memberships (user_id) where status = 'active';

-- A module the org has paid for. The same row gates both the API and the
-- navigation, so a module nobody paid for cannot be reached by typing a URL.
create table public.entitlements (
  org_id      uuid not null references public.organisations (id) on delete cascade,
  module      text not null check (module in (
                'stock','buy','freight','demand','cash','sell','money',
                'customers','books','people','intelligence')),
  status      text not null default 'active' check (status in ('active','trial','lapsed')),
  valid_until date,
  created_at  timestamptz not null default now(),
  primary key (org_id, module)
);

-- ---------------------------------------------------------------- helpers
--
-- All SECURITY DEFINER, all STABLE, all with search_path pinned: a definer
-- function with a mutable search_path can be hijacked by a caller-created
-- object of the same name.

create or replace function app.org_ids()
returns setof uuid
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select org_id from public.memberships
   where user_id = auth.uid() and status = 'active'
$$;

comment on function app.org_ids() is
  'Organisations the caller belongs to. SECURITY DEFINER so that a policy on
   memberships itself can call it without recursing into its own RLS.';

create or replace function app.has_role(target_org uuid, roles text[])
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1 from public.memberships
     where org_id = target_org
       and user_id = auth.uid()
       and status = 'active'
       and role = any (roles)
  )
$$;

-- An empty location_ids means every location, which is why this is not a
-- plain array containment test.
create or replace function app.can_see_location(target_org uuid, target_location uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1 from public.memberships m
     where m.org_id = target_org
       and m.user_id = auth.uid()
       and m.status = 'active'
       and (cardinality(m.location_ids) = 0 or target_location = any (m.location_ids))
  )
$$;

create or replace function app.has_module(target_org uuid, target_module text)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1 from public.entitlements e
     where e.org_id = target_org
       and e.module = target_module
       and e.status in ('active','trial')
       and (e.valid_until is null or e.valid_until >= current_date)
  )
$$;

grant usage on schema app to authenticated;
grant execute on function app.org_ids(), app.has_role(uuid, text[]),
  app.can_see_location(uuid, uuid), app.has_module(uuid, text) to authenticated;

-- ------------------------------------------------------------------- RLS
--
-- Every helper call is wrapped in (select …). Without the wrapper Postgres
-- re-evaluates it once per row; with it, the planner hoists it into an
-- InitPlan and runs it once per statement. On a ledger that is the
-- difference between a fast query and a timeout.

-- NOTE: never `for all` on a write policy. A FOR ALL policy's USING
-- clause also governs SELECT, and permissive policies are OR'd together,
-- so one `for all using (has_role(...))` silently re-opens read access to
-- anyone with the role -- bypassing both the module gate and the branch
-- scoping in the read policy above it. Split by command, every time.
alter table public.organisations enable row level security;
alter table public.locations     enable row level security;
alter table public.memberships   enable row level security;
alter table public.entitlements  enable row level security;

-- The `created_by` arm is load-bearing, not belt-and-braces. An INSERT
-- with RETURNING -- which is what supabase-js does by default when you
-- chain .select() -- has to pass the SELECT policy on the new row. The
-- membership that would grant that is created by an AFTER trigger, and
-- app.org_ids() is STABLE, so inside the same statement it still sees the
-- snapshot from before the trigger ran. Without this arm, creating an
-- organisation fails on the very first call a new user makes.
create policy org_read on public.organisations for select
  using (id in (select app.org_ids()) or created_by = (select auth.uid()));

create policy org_write on public.organisations for update
  using ((select app.has_role(id, array['owner'])))
  with check ((select app.has_role(id, array['owner'])));

-- Anyone signed in may create an organisation; the trigger below makes the
-- creator its owner, so the row is never left without one.
create policy org_create on public.organisations for insert
  with check (created_by = (select auth.uid()));

create policy loc_read on public.locations for select
  using (org_id in (select app.org_ids())
         and (select app.can_see_location(org_id, id)));

create policy loc_insert on public.locations for insert
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy loc_update on public.locations for update
  using ((select app.has_role(org_id, array['owner','manager'])))
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy loc_delete on public.locations for delete
  using ((select app.has_role(org_id, array['owner','manager'])));

-- A member sees their colleagues; only an owner changes who they are.
create policy mem_read on public.memberships for select
  using (user_id = (select auth.uid()) or org_id in (select app.org_ids()));

create policy mem_insert on public.memberships for insert
  with check ((select app.has_role(org_id, array['owner'])));
create policy mem_update on public.memberships for update
  using ((select app.has_role(org_id, array['owner'])))
  with check ((select app.has_role(org_id, array['owner'])));
create policy mem_delete on public.memberships for delete
  using ((select app.has_role(org_id, array['owner'])));

-- Entitlements are readable so the UI can hide what is not paid for, and
-- writable by nobody: billing changes them through the service role.
create policy ent_read on public.entitlements for select
  using (org_id in (select app.org_ids()));

-- ------------------------------------------------------- creating an org

create or replace function app.claim_new_organisation()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  insert into public.memberships (org_id, user_id, role)
  values (new.id, new.created_by, 'owner');

  -- The importer wedge, free while the product is finding its feet.
  insert into public.entitlements (org_id, module, status)
  select new.id, m, 'trial'
    from unnest(array['stock','buy','freight','demand','cash']) as m;

  return new;
end;
$$;

create trigger organisations_claim
  after insert on public.organisations
  for each row execute function app.claim_new_organisation();
