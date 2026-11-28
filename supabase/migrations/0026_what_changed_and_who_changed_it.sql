-- What changed, who changed it, and what it was before.
--
-- An item's lead time can go from 120 days to 12 in one keystroke on
-- the stock grid. That figure feeds the reorder point, which feeds
-- the order quantity, which feeds the cash plan -- so the whole
-- business buys differently from that moment, and nothing anywhere
-- recorded that it had happened, let alone what the number used to
-- be. The same is true of a credit limit, a list price, a supplier's
-- lead time and a branch's name.
--
-- The product already audited two things, and audited them well:
-- shipment weights and volumes (0019), and receipt corrections,
-- which are reversals carrying their own history. Both were written
-- because they move money between items. So do the figures above;
-- they just do it more slowly, which is why nobody noticed the gap.
--
-- A trigger rather than application code, for the same reason the
-- append-only ledger is a trigger rather than a policy: the rule
-- then holds for every path into the table. The endpoint, the
-- importer, a service-role script, somebody in the SQL console at
-- two in the morning -- all of them, not only the path somebody
-- remembered to instrument. An audit with one uninstrumented path
-- is worse than none, because it is believed.
--
-- UPDATE and DELETE only. Those are the two that destroy
-- information: afterwards the row cannot tell you what it was, or
-- is not there to ask. A creation is visible as the row itself, and
-- the tables that matter carry created_at. Recording inserts as
-- well would double the size of this table to say what a person can
-- already see.

create table public.record_changes (
  id           uuid primary key default gen_random_uuid(),
  org_id       uuid not null references public.organisations (id)
                    on delete cascade,
  -- Not a foreign key to the row. The point of this table is to
  -- outlive the row, and a reference would either block the delete
  -- or cascade away the very record of it.
  table_name   text not null,
  record_id    uuid not null,
  -- What the row calls itself, captured at the time: 'RGT-100', not
  -- a uuid nobody can read, and not a join that returns nothing once
  -- the row is gone.
  record_label text,
  -- One row per field that moved. '(deleted)' for a whole row going,
  -- where `was` carries the label and the row's own identity.
  field        text not null,
  was          text,
  became       text,
  changed_at   timestamptz not null default now(),
  changed_by   uuid references auth.users (id) default auth.uid()
);

create index record_changes_record_idx
  on public.record_changes (org_id, table_name, record_id,
                            changed_at desc);
create index record_changes_recent_idx
  on public.record_changes (org_id, changed_at desc);

comment on table public.record_changes is
  'What changed on a business record, who changed it and what it was '
  'before. Written by trigger, so it holds for the importer and a '
  'service-role script as well as for the endpoints. UPDATE and '
  'DELETE only: those are the two that destroy information.';

alter table public.record_changes enable row level security;

-- Readable by the organisation it belongs to, and by nobody else.
-- The rows carry field values, so a leak here is a leak of
-- everything the audit covers.
create policy rc_read on public.record_changes for select
  using (org_id in (select app.org_ids()));

-- No insert, update or delete policy, on purpose. The trigger runs
-- as the function's owner and writes regardless; a signed-in user
-- has no way to add a line to the history or to take one out. An
-- audit anybody can edit is a note, not an audit.

-- The fields worth a line each. Deliberately not "every column":
-- created_at moving is not a change anybody asked about, and an
-- audit that lists noise is one nobody reads.
--
-- The test is whether the figure reaches a decision. A lead time
-- reaches the reorder point; a credit limit reaches whether the next
-- order ships; a list price reaches the margin. A barcode does not,
-- and is left out.
create or replace function app.watched_fields(t text)
returns text[]
language sql
immutable
as $$
  select case t
    when 'items' then array[
      'sku', 'description', 'category', 'uom', 'lead_time_days',
      'lead_time_days_std', 'is_clinical_critical', 'list_price',
      'is_generic', 'is_active', 'supplier_id']
    when 'suppliers' then array[
      'code', 'name', 'country', 'currency', 'lead_time_days']
    when 'customers' then array[
      'code', 'name', 'payment_terms_days', 'credit_limit',
      'is_public_sector', 'is_intercompany']
    when 'locations' then array['code', 'name', 'is_active',
                                'is_warehouse']
    when 'purchase_orders' then array[
      'po_number', 'supplier_id', 'location_id', 'currency',
      'fx_rate', 'status', 'expected_at']
    when 'po_lines' then array['qty', 'unit_price']
    when 'shipments' then array[
      'reference', 'supplier_id', 'mode', 'status', 'etd', 'eta']
    when 'shipment_costs' then array[
      'kind', 'allocate_by', 'amount', 'currency', 'fx_rate']
    when 'employees' then array[
      'employee_no', 'name', 'department', 'basic_monthly',
      'allowances_monthly', 'medical_monthly', 'employer_pf_rate',
      'employee_pf_rate', 'is_active', 'left_on']
    when 'machines' then array[
      'machine_key', 'customer_id', 'location_id', 'capital_cost',
      'contract_months', 'expected_monthly_tests', 'revenue_per_test',
      'reagent_margin_pct', 'status']
    else array[]::text[]
  end;
$$;

-- The column a person would use to name the row.
create or replace function app.record_label(t text, row_json jsonb)
returns text
language sql
immutable
as $$
  select coalesce(
    case t
      when 'items'           then row_json ->> 'sku'
      when 'suppliers'       then row_json ->> 'code'
      when 'customers'       then row_json ->> 'code'
      when 'locations'       then row_json ->> 'code'
      when 'purchase_orders' then row_json ->> 'po_number'
      when 'shipments'       then row_json ->> 'reference'
      when 'employees'       then row_json ->> 'employee_no'
      when 'machines'        then row_json ->> 'machine_key'
    end,
    -- A line on something has no name of its own, so it borrows its
    -- parent's. Better than a uuid and honest about what it is.
    case t
      when 'po_lines'       then 'line on ' || (row_json ->> 'po_id')
      when 'shipment_costs' then 'charge on ' ||
                                 (row_json ->> 'shipment_id')
    end
  );
$$;

create or replace function app.note_record_change()
returns trigger
language plpgsql
-- security definer, so the write happens whatever the caller's
-- policies say. The read policy above still decides who can see it.
security definer
set search_path = public, pg_catalog
as $$
declare
  t      text := tg_table_name;
  before jsonb;
  after  jsonb;
  f      text;
  old_v  text;
  new_v  text;
begin
  if tg_op = 'DELETE' then
    before := to_jsonb(old);
    -- Only while the organisation is still there.
    --
    -- Deleting an organisation cascades to every table below it, and
    -- each of those deletes fires this trigger -- which then tried
    -- to write a history row referencing the organisation that is
    -- part-way through being removed, and failed on the foreign key,
    -- taking the whole delete with it. An organisation leaving takes
    -- its history with it by the same cascade, so there is nothing
    -- to record and nowhere to record it.
    insert into public.record_changes
      (org_id, table_name, record_id, record_label, field, was, became)
    select (before ->> 'org_id')::uuid, t, (before ->> 'id')::uuid,
           app.record_label(t, before), '(deleted)',
           -- The whole row, so the one thing that cannot be looked
           -- up afterwards is the thing that was lost.
           before::text, null
     where exists (select 1 from public.organisations
                    where id = (before ->> 'org_id')::uuid);
    return old;
  end if;

  before := to_jsonb(old);
  after  := to_jsonb(new);
  foreach f in array app.watched_fields(t) loop
    old_v := before ->> f;
    new_v := after  ->> f;
    -- `is distinct from`, so null to a value and a value to null
    -- both count. Plain <> treats either as unknown and records
    -- nothing, which is the half of the changes most worth having.
    if old_v is distinct from new_v then
      insert into public.record_changes
        (org_id, table_name, record_id, record_label, field, was, became)
      values ((after ->> 'org_id')::uuid, t, (after ->> 'id')::uuid,
              app.record_label(t, after), f, old_v, new_v);
    end if;
  end loop;
  return new;
end;
$$;

comment on function app.note_record_change() is
  'Writes one public.record_changes row per watched field that '
  'moved, and one for a deleted row. Attached to the tables listed '
  'in app.watched_fields. NOT attached to stock_movement, which '
  'refuses update and delete outright -- a trigger watching for '
  'them there would read as though the ledger could be edited.';

do $$
declare
  t text;
begin
  foreach t in array array[
    'items', 'suppliers', 'customers', 'locations', 'purchase_orders',
    'po_lines', 'shipments', 'shipment_costs', 'employees', 'machines'
  ] loop
    execute format(
      'drop trigger if exists %I on public.%I',
      'record_change_' || t, t);
    execute format(
      'create trigger %I after update or delete on public.%I'
      ' for each row execute function app.note_record_change()',
      'record_change_' || t, t);
  end loop;
end
$$;

-- A view that reads as a sentence, for the screen and for anybody at
-- a psql prompt trying to work out when a figure moved.
create or replace view public.record_history
with (security_invoker = true) as
  select c.org_id,
         c.changed_at,
         c.table_name,
         c.record_label,
         c.field,
         c.was,
         c.became,
         c.changed_by,
         coalesce(m.email, '(unknown)') as changed_by_email
    from public.record_changes c
    left join public.memberships m
      on m.user_id = c.changed_by and m.org_id = c.org_id;

comment on view public.record_history is
  'public.record_changes with the person''s email joined on, newest '
  'first when ordered by changed_at desc.';
