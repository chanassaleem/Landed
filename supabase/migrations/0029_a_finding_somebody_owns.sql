-- Turning a list of findings into work that gets done.
--
-- Checks produced a good list and no way to act on it. Two people
-- look at the same twelve findings, each assumes the other is on
-- the third one, and the list reads identically next week.
--
-- Two states, and they say different things. Assigned is who is on
-- it. Accepted is that the business has looked and this is how
-- things are: a branch really does hold two items with no cost
-- because they are free samples, and reporting that every week
-- trains people to scroll past the whole list.
--
-- Resolved is deliberately absent. A finding is computed from the
-- data; when the data is fixed the check stops producing it.
-- Storing "resolved" would be a second answer to "is this still
-- wrong", and the stored one would go stale first -- which is the
-- same reasoning that keeps stock on hand out of a column.
--
-- Acceptance is the one that needs care. Accepting "items with no
-- unit cost" and then adding a new item with no cost must not hide
-- the new one; that is how an accepted finding becomes a
-- blindfold. So an acceptance records the rows it covered, and the
-- screen reopens the finding when it names a row outside that set.

create table public.check_dispositions (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id)
                   on delete cascade,
  -- The check's code, as engine/sanity.py produces it. Not a
  -- reference to a findings table, because there is no findings
  -- table: a finding exists only while the data makes it true.
  code        text not null,
  state       text not null check (state in ('assigned', 'accepted')),
  assigned_to uuid references auth.users (id) on delete set null,
  why         text,
  -- The rows the acceptance covered. A finding naming anything
  -- outside this set is not the finding that was accepted.
  accepted_rows text[] not null default '{}',
  decided_at  timestamptz not null default now(),
  decided_by  uuid references auth.users (id) default auth.uid()
);

create unique index check_dispositions_code_idx
  on public.check_dispositions (org_id, code);

comment on table public.check_dispositions is
  'Who is on a data-quality finding, or why it has been accepted as '
  'how things are. One row per check code per organisation. There '
  'is no resolved state: a fixed finding stops being produced.';

alter table public.check_dispositions enable row level security;

create policy cd_read on public.check_dispositions for select
  using (org_id in (select app.org_ids()));
create policy cd_insert on public.check_dispositions for insert
  with check (org_id in (select app.org_ids())
              and (select app.has_role(org_id,
                     array['owner', 'manager', 'accountant'])));
create policy cd_update on public.check_dispositions for update
  using (org_id in (select app.org_ids())
         and (select app.has_role(org_id,
                array['owner', 'manager', 'accountant'])));
create policy cd_delete on public.check_dispositions for delete
  using (org_id in (select app.org_ids())
         and (select app.has_role(org_id, array['owner', 'manager'])));
