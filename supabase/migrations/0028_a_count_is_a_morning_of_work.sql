-- Counting a branch as one piece of work.
--
-- The per-line count adjustment is sound and stays: the person
-- types what is on the shelf, the ledger reconciles to it, and an
-- advisory lock stops two counts racing. What was missing is the
-- thing around it.
--
-- A stocktake is a morning. Somebody walks the shelves with a list,
-- and the figures they write down have to be compared against what
-- the system believed when they started -- not against what it
-- believes when they finish, because by then the afternoon's sales
-- have gone through and every variance is wrong by a day of
-- trading. A line-at-a-time adjustment cannot hold that, because
-- each one reconciles to the ledger as it stands at that instant.
--
-- So the expected figure is captured when the session opens, the
-- counts are held while they are being taken, the variances can be
-- read before anything is written, and the lot posts together.
-- Until it posts nothing has touched the ledger, which is what
-- makes it safe to count over several hours and stop for lunch.

create table public.stocktakes (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id)
                   on delete cascade,
  location_id uuid not null references public.locations (id)
                   on delete cascade,
  reference   text not null,
  status      text not null default 'open'
                   check (status in ('open', 'posted', 'abandoned')),
  note        text,
  opened_at   timestamptz not null default now(),
  opened_by   uuid references auth.users (id) default auth.uid(),
  closed_at   timestamptz,
  closed_by   uuid references auth.users (id)
);

create unique index stocktakes_reference_idx
  on public.stocktakes (org_id, upper(reference));

-- One open count per branch. Two is two people writing different
-- numbers against the same shelf, and whichever posts second wins
-- silently.
create unique index stocktakes_one_open_per_branch_idx
  on public.stocktakes (org_id, location_id)
  where status = 'open';

comment on table public.stocktakes is
  'A physical count of one branch, held while it is being taken. '
  'Nothing reaches the ledger until it is posted.';

create table public.stocktake_lines (
  id           uuid primary key default gen_random_uuid(),
  org_id       uuid not null references public.organisations (id)
                    on delete cascade,
  stocktake_id uuid not null references public.stocktakes (id)
                    on delete cascade,
  item_id      uuid not null references public.items (id)
                    on delete cascade,
  -- What the ledger said when this line was put on the list. The
  -- whole point: a variance is against the figure the counter was
  -- working from.
  expected     numeric(18,4) not null default 0,
  -- Null until somebody counts it. Not counted and counted as zero
  -- are different facts, and treating the first as the second
  -- writes off stock nobody looked at.
  counted      numeric(18,4) check (counted is null or counted >= 0),
  note         text,
  counted_at   timestamptz,
  counted_by   uuid references auth.users (id)
);

create unique index stocktake_lines_item_idx
  on public.stocktake_lines (org_id, stocktake_id, item_id);

alter table public.stocktakes      enable row level security;
alter table public.stocktake_lines enable row level security;

-- Split by command, never `for all` -- see the note in 0002.
create policy st_read on public.stocktakes for select
  using (org_id in (select app.org_ids())
         and (select app.has_module(org_id, 'stock'))
         and (select app.can_see_location(org_id, location_id)));
create policy st_insert on public.stocktakes for insert
  with check (org_id in (select app.org_ids())
              and (select app.has_role(org_id,
                     array['owner', 'manager', 'staff']))
              and (select app.can_see_location(org_id, location_id)));
create policy st_update on public.stocktakes for update
  using (org_id in (select app.org_ids())
         and (select app.has_role(org_id,
                array['owner', 'manager', 'staff']))
         and (select app.can_see_location(org_id, location_id)));

create policy stl_read on public.stocktake_lines for select
  using (org_id in (select app.org_ids())
         and (select app.has_module(org_id, 'stock')));
create policy stl_insert on public.stocktake_lines for insert
  with check (org_id in (select app.org_ids())
              and (select app.has_role(org_id,
                     array['owner', 'manager', 'staff'])));
create policy stl_update on public.stocktake_lines for update
  using (org_id in (select app.org_ids())
         and (select app.has_role(org_id,
                array['owner', 'manager', 'staff'])));
create policy stl_delete on public.stocktake_lines for delete
  using (org_id in (select app.org_ids())
         and (select app.has_role(org_id, array['owner', 'manager'])));

-- A count with its variances, and whether the shelf has moved since
-- the line was counted.
--
-- The physical count still wins at posting -- that is what a
-- stocktake is -- but a variance explained by a sale that went
-- through while the counter was three aisles away is a different
-- conversation from one that is not.
create view public.stocktake_variance
with (security_invoker = true) as
  with live as (
    select m.org_id, m.item_id, m.location_id,
           sum(m.qty) as on_hand
      from public.stock_movement m
     group by m.org_id, m.item_id, m.location_id
  )
  select s.org_id,
         s.id                        as stocktake_id,
         s.reference,
         s.status,
         l.code                      as branch,
         i.sku,
         i.description,
         sl.id                       as line_id,
         sl.expected,
         sl.counted,
         case when sl.counted is null then null
              else sl.counted - sl.expected end        as variance,
         coalesce(live.on_hand, 0)                     as on_hand_now,
         -- Moved under the counter: the ledger is no longer at the
         -- figure this line was measured against.
         (coalesce(live.on_hand, 0) <> sl.expected)    as moved_since,
         sl.counted_at,
         sl.counted_by
    from public.stocktake_lines sl
    join public.stocktakes s on s.id = sl.stocktake_id
    join public.items i      on i.id = sl.item_id
    join public.locations l  on l.id = s.location_id
    left join live
      on live.item_id = sl.item_id
     and live.location_id = s.location_id
     and live.org_id = sl.org_id;

comment on view public.stocktake_variance is
  'A stocktake''s lines with what was expected, what was counted, '
  'the difference, and whether the shelf has moved since the line '
  'was counted. moved_since does not change what posting does -- '
  'the count wins -- it changes what the variance means.';
