-- Why a charge did not reach a price, and how to make it.
--
-- Every cost on a shipment is divided across the lines of that
-- shipment on its own basis: a shipping line bills by volume, customs
-- by value, a haulier by weight. The division is a share -- this
-- line's weight over the shipment's weight -- so when the shipment's
-- weight is nothing, the share is a division by zero. The database
-- refuses it and the allocation comes out null, which is correct and
-- silent: the bill still sits in the shipment total, and not one unit
-- price moves. An importer pricing off that stock is short by the
-- whole freight bill and has nothing on any screen to tell them.
--
-- The screens already showed how much had been allocated and how much
-- had not. What nobody could see was which charge, and why, and what
-- to do about it -- and those three are the difference between a
-- number that worries somebody and a number they can act on.
--
-- So: one row per charge, saying what was billed, what reached the
-- goods, and when nothing did, which fact is missing. Named here
-- rather than worked out on a screen, because the Landing screen, the
-- data-quality module and the export would each have had to derive it
-- from the allocation rules, and three derivations of one rule is how
-- they come to disagree.

create view public.shipment_charge_allocation
with (security_invoker = true) as
  select c.org_id,
         c.shipment_id,
         s.reference,
         s.status                      as shipment_status,
         c.id                          as charge_id,
         c.kind,
         c.allocate_by,
         c.amount,
         c.currency,
         c.fx_rate,
         c.amount * c.fx_rate          as billed,
         -- A share divides out to exactly one whenever the basis it
         -- divides by is not zero, so a charge either lands whole or
         -- does not land at all. There is no partial case to report.
         case when coalesce(t.qty, 0) = 0 then 0
              when c.allocate_by = 'value'
                   and coalesce(t.fob_value, 0) = 0  then 0
              when c.allocate_by = 'weight'
                   and coalesce(t.weight_kg, 0) = 0  then 0
              when c.allocate_by = 'volume'
                   and coalesce(t.volume_cbm, 0) = 0 then 0
              else c.amount * c.fx_rate
         end                           as allocated,
         case when coalesce(t.qty, 0) = 0 then c.amount * c.fx_rate
              when c.allocate_by = 'value'
                   and coalesce(t.fob_value, 0) = 0  then c.amount * c.fx_rate
              when c.allocate_by = 'weight'
                   and coalesce(t.weight_kg, 0) = 0  then c.amount * c.fx_rate
              when c.allocate_by = 'volume'
                   and coalesce(t.volume_cbm, 0) = 0 then c.amount * c.fx_rate
              else 0
         end                           as unallocated,
         -- The missing fact, in the words of the thing somebody has to
         -- go and do. 'awaiting goods' is not a fault: a shipment at
         -- sea has no received lines yet and should not be flagged as
         -- though somebody forgot something. The caller decides that,
         -- using shipment_status beside it.
         case when coalesce(t.qty, 0) = 0 then 'awaiting goods'
              when c.allocate_by = 'value'
                   and coalesce(t.fob_value, 0) = 0  then 'no goods value'
              when c.allocate_by = 'weight'
                   and coalesce(t.weight_kg, 0) = 0  then 'no weight recorded'
              when c.allocate_by = 'volume'
                   and coalesce(t.volume_cbm, 0) = 0 then 'no volume recorded'
         end                           as blocked_by
    from public.shipment_costs c
    join public.shipments s on s.id = c.shipment_id
    left join public.shipment_totals t
           on t.shipment_id = c.shipment_id and t.org_id = c.org_id;

comment on view public.shipment_charge_allocation is
  'One row per charge on a shipment: billed, allocated, unallocated, '
  'and when nothing landed, which fact is missing. A charge divided by '
  'a basis of zero allocates to nothing, correctly and silently, and '
  'the importer prices stock short by the whole bill. blocked_by names '
  'what to go and enter. awaiting goods is not a fault.';

-- What was changed about a shipment line, by whom, and why.
--
-- Weight and volume are the figures freight is divided by, so
-- correcting one moves money between items. The allocation itself is
-- a view over the current numbers, which means the old division
-- disappears the moment the number changes -- so if the change is not
-- recorded here, there is no way afterwards to explain why last
-- month's landed cost was what it was.
create table public.shipment_line_changes (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id)
                   on delete cascade,
  shipment_id uuid not null references public.shipments (id)
                   on delete cascade,
  grn_line_id uuid not null references public.grn_lines (id)
                   on delete cascade,
  field       text not null check (field in ('weight_kg', 'volume_cbm')),
  was         numeric(18,6),
  became      numeric(18,6),
  why         text not null,
  changed_at  timestamptz not null default now(),
  changed_by  uuid references auth.users (id) default auth.uid()
);

create index shipment_line_changes_line_idx
  on public.shipment_line_changes (org_id, grn_line_id, changed_at desc);

alter table public.shipment_line_changes enable row level security;

-- Split by command, every time. A FOR ALL policy's USING clause also
-- governs SELECT, and permissive policies are OR'd, so one
-- `for all using (has_role(...))` silently re-opens read access to
-- anyone with the role.
create policy slc_read on public.shipment_line_changes for select
  using (org_id in (select app.org_ids())
         and (select app.has_module(org_id, 'buy')));
create policy slc_insert on public.shipment_line_changes for insert
  with check ((select app.has_role(org_id, array['owner','manager'])));
-- No update or delete policy: an audit trail that can be edited is
-- not one. Rows are only ever added.

comment on table public.shipment_line_changes is
  'Audit trail for the weight and volume freight is divided by. The '
  'allocation is a view over the current figures, so the division that '
  'produced last month landed cost is gone the moment a figure '
  'changes; this is what explains it afterwards.';
