-- Read policies that do not call a function per row.
--
-- Measured, not guessed. On 2,000 items across 6 branches with 300,000
-- movements -- well past what the first customers will have -- loading
-- the main screen took 5.9 seconds, and the schema was blamed for it.
-- The same queries run as the table owner, with the policies skipped,
-- took 0.73s. The joins were never the problem; the policies were 8x
-- the cost of the work.
--
-- The reason is subtle and worth writing down, because the code looked
-- like it had already dealt with it. Migration 0001 wraps every helper
-- call in `(select ...)`, which lets the planner hoist it into an
-- InitPlan and run it once. That works for a call with no row
-- references, like app.org_ids(). It does nothing for
-- app.can_see_location(org_id, location_id): a subquery that reads the
-- row's own columns is correlated, cannot be hoisted, and runs per row
-- -- 300,000 SECURITY DEFINER calls, each querying memberships.
--
-- So the same rules are expressed as sets. `location_id in (select
-- app.visible_locations())` is computed once per statement and then
-- becomes a hash lookup. Nothing about who can see what changes, which
-- is the whole point: tests/test_rls.py passes unaltered, and it is
-- the reason this migration is safe to apply.
--
-- Result: 5.9s -> 1.2s, same rows.

begin;

-- Every location this caller may see, across every organisation they
-- belong to. Same rule as app.can_see_location: an empty location_ids
-- on the membership means all of that organisation's branches.
create or replace function app.visible_locations()
returns setof uuid
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select l.id
    from public.locations l
    join public.memberships m
      on m.org_id = l.org_id
     and m.user_id = auth.uid()
     and m.status = 'active'
   where cardinality(m.location_ids) = 0
      or l.id = any (m.location_ids)
$$;

comment on function app.visible_locations() is
  'The set form of app.can_see_location, for use in a policy as
   `location_id in (select app.visible_locations())`. Identical rule,
   evaluated once per statement instead of once per row.';

-- Organisations where a module is live. Same rule as app.has_module,
-- including trial status and the validity date -- both were dropped in
-- a first draft of this, which made the policy quietly stricter and
-- returned no rows at all.
create or replace function app.orgs_with_module(target_module text)
returns setof uuid
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select e.org_id
    from public.entitlements e
   where e.module = target_module
     and e.status in ('active','trial')
     and (e.valid_until is null or e.valid_until >= current_date)
$$;

comment on function app.orgs_with_module(text) is
  'The set form of app.has_module. Identical rule, evaluated once per
   statement instead of once per row.';

grant execute on function app.visible_locations(),
                         app.orgs_with_module(text) to authenticated;

-- ------------------------------------------------------- the hot path
--
-- Only SELECT policies, and only on the tables a screen reads in bulk.
-- A write policy is checked against the handful of rows being written,
-- so a per-row call there costs nothing worth a migration.

drop policy if exists mv_read on public.stock_movement;
create policy mv_read on public.stock_movement for select
  using (org_id in (select app.org_ids())
         and location_id in (select app.visible_locations())
         and org_id in (select app.orgs_with_module('stock')));

drop policy if exists dh_read on public.demand_history;
create policy dh_read on public.demand_history for select
  using (org_id in (select app.org_ids())
         and location_id in (select app.visible_locations()));

drop policy if exists item_read on public.items;
create policy item_read on public.items for select
  using (org_id in (select app.org_ids())
         and org_id in (select app.orgs_with_module('stock')));

drop policy if exists sup_read on public.suppliers;
create policy sup_read on public.suppliers for select
  using (org_id in (select app.org_ids())
         and org_id in (select app.orgs_with_module('buy')));

commit;
