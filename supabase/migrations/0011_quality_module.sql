-- A fourteenth module: the data-quality checks.
--
-- engine/sanity.py has run on every import since the beginning and its
-- findings reached nobody. They went into the command-line report and
-- stopped there -- 657 lines of checks for the faults that make a
-- figure wrong rather than missing: a unit cost out by a hundred, two
-- spellings of one customer, a goods receipt dated before the order it
-- belongs to.
--
-- Hiding that half is the wrong way round. A confidently wrong number
-- is worse than an absent one, because somebody acts on it.
--
-- Added in its own migration rather than by editing 0009. That one has
-- been applied; a constraint edited in place would be a file that no
-- longer describes what any existing database actually has.

begin;

alter table public.entitlements drop constraint entitlements_module_check;
alter table public.entitlements add constraint entitlements_module_check
  check (module in (
    'stock','buy','freight','demand','cash','sell','money',
    'customers','books','people','intelligence','equipment','service',
    'quality'));

create or replace function app.claim_new_organisation()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  insert into public.memberships (org_id, user_id, role)
  values (new.id, new.created_by, 'owner');

  insert into public.entitlements (org_id, module, status)
  select new.id, m, 'trial'
    from unnest(array[
      'stock','buy','freight','demand','cash','sell','money',
      'customers','books','people','intelligence','equipment','service',
      'quality'
    ]) as m
  on conflict (org_id, module) do nothing;

  return new;
end;
$$;

insert into public.entitlements (org_id, module, status)
select o.id, 'quality', 'trial' from public.organisations o
on conflict (org_id, module) do nothing;

commit;
