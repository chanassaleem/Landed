-- Customers, what they owe, the machines placed with them, and the
-- jobs done on those machines.
--
-- The engine already computes all of this -- engine/modules/
-- receivables.py, machines.py and service.py, with 47 tests between
-- them. What was missing was somewhere to put the inputs. Every column
-- here exists because one of those modules reads it; nothing is here
-- speculatively.
--
-- The machines table is the one no off-the-shelf system holds. A
-- stock system tracks the analyser as a unit sold, an accounting
-- system tracks the invoice, and a maintenance system tracks the
-- visits. None of them can tell you whether the analyser you placed
-- free of charge has earned back its cost in reagent pull, which for
-- this trade is the whole question.

begin;

create table public.customers (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  code        text not null,
  name        text not null,
  city        text,
  -- Public-sector debtors pay late as a matter of course, and mixing
  -- them into one DSO figure hides whether the private book is healthy.
  is_public_sector boolean not null default false,
  -- A sale to another company you own is not money owed. Counted in
  -- the total it flatters the figure and nobody notices.
  is_intercompany  boolean not null default false,
  credit_limit numeric(18,2),
  payment_terms_days integer not null default 30,
  contact_name  text,
  contact_email text,
  contact_phone text,
  custom      jsonb not null default '{}'::jsonb,
  is_active   boolean not null default true,
  created_at  timestamptz not null default clock_timestamp(),
  unique (org_id, code)
);

create table public.invoices (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  customer_id uuid not null references public.customers (id) on delete cascade,
  location_id uuid references public.locations (id),
  invoice_no  text not null,
  invoice_date date not null,
  due_date    date not null,
  amount      numeric(18,2) not null check (amount >= 0),
  -- Paid to date rather than a status. A status cannot express a part
  -- payment, and a part payment is most of this book.
  amount_paid numeric(18,2) not null default 0 check (amount_paid >= 0),
  currency    text not null default 'PKR' check (length(currency) = 3),
  note        text,
  created_at  timestamptz not null default clock_timestamp(),
  unique (org_id, invoice_no)
);

create index invoices_open_idx on public.invoices (org_id, due_date)
  where amount_paid < amount;

create table public.machines (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  customer_id uuid references public.customers (id) on delete set null,
  location_id uuid references public.locations (id),
  machine_key text not null,
  model       text not null,
  serial_no   text,
  placed_date date,
  -- What it cost you to put it there. Zero for a machine the customer
  -- bought; the whole point of the record for one you placed free.
  capital_cost numeric(18,2) not null default 0,
  contract_months integer not null default 36,
  expected_monthly_tests numeric(14,2) not null default 0,
  revenue_per_test numeric(14,4) not null default 0,
  reagent_margin_pct numeric(6,4) not null default 0,
  status      text not null default 'placed'
              check (status in ('placed','returned','sold','retired')),
  custom      jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default clock_timestamp(),
  unique (org_id, machine_key)
);

create table public.service_jobs (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  machine_id  uuid references public.machines (id) on delete set null,
  location_id uuid references public.locations (id),
  job_no      text not null,
  opened_date date not null,
  closed_date date,
  -- Hours worked and hours actually invoiced, kept apart. Held as one
  -- number, the gap between them -- which is the entire finding of
  -- engine/modules/service.py -- cannot be computed at all.
  labour_hours        numeric(10,2) not null default 0,
  labour_hours_billed numeric(10,2) not null default 0,
  parts_cost          numeric(18,2) not null default 0,
  parts_charged       numeric(18,2) not null default 0,
  is_warranty      boolean not null default false,
  is_repeat_visit  boolean not null default false,
  downtime_days    numeric(8,2) not null default 0,
  note        text,
  created_at  timestamptz not null default clock_timestamp(),
  unique (org_id, job_no)
);

create index service_jobs_machine_idx on public.service_jobs (org_id, machine_id);

-- -------------------------------------------------------------- RLS
--
-- Same shape as everywhere else, and the same rule: never `for all` on
-- a write policy. A FOR ALL policy's USING clause also governs SELECT,
-- and permissive policies are OR'd, so one of them silently reopens
-- read access to anyone holding the role.
--
-- Read policies use the set form from 0006 -- `org_id in (select
-- app.orgs_with_module(...))` -- rather than a per-row function call,
-- which is what made the main screen eight times slower.

alter table public.customers    enable row level security;
alter table public.invoices     enable row level security;
alter table public.machines     enable row level security;
alter table public.service_jobs enable row level security;

create policy cust_read on public.customers for select
  using (org_id in (select app.org_ids()));
create policy cust_insert on public.customers for insert
  with check ((select app.has_role(org_id, array['owner','manager','staff'])));
create policy cust_update on public.customers for update
  using ((select app.has_role(org_id, array['owner','manager'])))
  with check ((select app.has_role(org_id, array['owner','manager'])));

create policy inv_read on public.invoices for select
  using (org_id in (select app.org_ids())
         and org_id in (select app.orgs_with_module('cash')));
create policy inv_insert on public.invoices for insert
  with check ((select app.has_role(org_id, array['owner','manager','accountant']))
              and (select app.has_module(org_id, 'cash')));
create policy inv_update on public.invoices for update
  using ((select app.has_role(org_id, array['owner','manager','accountant'])))
  with check ((select app.has_role(org_id, array['owner','manager','accountant'])));

create policy mach_read on public.machines for select
  using (org_id in (select app.org_ids()));
create policy mach_insert on public.machines for insert
  with check ((select app.has_role(org_id, array['owner','manager'])));
create policy mach_update on public.machines for update
  using ((select app.has_role(org_id, array['owner','manager'])))
  with check ((select app.has_role(org_id, array['owner','manager'])));

create policy job_read on public.service_jobs for select
  using (org_id in (select app.org_ids()));
create policy job_insert on public.service_jobs for insert
  with check ((select app.has_role(org_id, array['owner','manager','staff'])));
create policy job_update on public.service_jobs for update
  using ((select app.has_role(org_id, array['owner','manager','staff'])))
  with check ((select app.has_role(org_id, array['owner','manager','staff'])));

grant select, insert, update on public.customers, public.invoices,
  public.machines, public.service_jobs to authenticated;

commit;
