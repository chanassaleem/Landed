-- The tables the rest of the engine has been waiting for, and the way
-- a second person gets into an organisation.
--
-- Four modules were fully written and tested with nowhere to read
-- from: engine/modules/finance.py (statements), marketing.py
-- (pipeline), engine/payroll.py (payroll) and the commitments half of
-- cash.py. Between them that is 1,100 lines of arithmetic and four
-- test files, reachable only from the command-line report. This
-- migration gives each one its inputs.
--
-- Column names match the dataclass fields the modules already read,
-- deliberately and exactly. Where they differ a translation layer
-- appears in the reader, and a translation layer is where a rename
-- quietly becomes a wrong number.

begin;

-- Equipment and service were screens in the app with no entitlement to
-- license them, so every organisation saw them whatever it had paid
-- for. The constraint is the licence, so it has to know they exist.
alter table public.entitlements drop constraint entitlements_module_check;
alter table public.entitlements add constraint entitlements_module_check
  check (module in (
    'stock','buy','freight','demand','cash','sell','money',
    'customers','books','people','intelligence','equipment','service'));


-- ─────────────────────────────────────────── books: the trial balance

-- One row per account per period, in natural positive amounts. The
-- `line` column maps the account to a statement line explicitly
-- because engine/modules/finance.py refuses to guess: "insurance"
-- could be an expense or a prepayment, and guessing puts the
-- difference in profit.
create table public.accounts (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  code        text not null,
  name        text not null,
  line        text not null check (line in (
                'revenue','cogs','payroll','opex','depreciation',
                'finance_cost','other_income','tax',
                'cash','receivables','inventory','other_current_assets',
                'fixed_assets','accumulated_depreciation',
                'payables','short_term_debt','long_term_debt',
                'other_liabilities','tax_payable',
                'equity','retained_earnings')),
  -- A trial balance is as at a date. Holding the period on the row
  -- rather than in a separate header table means a balance can never
  -- be orphaned from the period it belongs to.
  period_from date not null,
  period_to   date not null check (period_to >= period_from),
  opening     numeric(18,2) not null default 0,
  closing     numeric(18,2) not null default 0,
  created_at  timestamptz not null default clock_timestamp(),
  unique (org_id, code, period_to)
);

create index accounts_period_idx on public.accounts (org_id, period_to);

-- A dividend is the one movement in equity that is not profit, and the
-- retained-earnings check in finance.py needs it or it reports a
-- failure every time a company pays one.
create table public.equity_movements (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  kind        text not null check (kind in ('dividend','capital_injection','other')),
  moved_on    date not null,
  amount      numeric(18,2) not null,
  note        text,
  created_at  timestamptz not null default clock_timestamp()
);


-- ──────────────────────────────────── cash: what is already committed

-- Without these the 13-week view is fiction: the letter of credit
-- settles, the bank is repaid and the salaries go out whatever the
-- stock position says.
create table public.commitments (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  location_id uuid references public.locations (id),
  label       text not null,
  kind        text not null check (kind in (
                'lc_settlement','loan_repayment','payroll','rent','tax',
                'supplier','other')),
  due_date    date not null,
  amount      numeric(18,2) not null check (amount >= 0),
  -- A recurring commitment is held once and expanded across the
  -- horizon by the engine. Held as thirteen rows it goes stale the
  -- moment the rent changes.
  is_recurring      boolean not null default false,
  recurs_every_days integer not null default 0 check (recurs_every_days >= 0),
  is_settled  boolean not null default false,
  created_at  timestamptz not null default clock_timestamp()
);

create index commitments_due_idx on public.commitments (org_id, due_date)
  where is_settled = false;

-- The opening bank balance the cash view starts from. Held as a dated
-- series rather than a single settings value so that last week's view
-- can still be reproduced, and so nobody has to remember to update it.
create table public.cash_balances (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  as_at       date not null,
  balance     numeric(18,2) not null,
  account_label text not null default 'Main account',
  created_at  timestamptz not null default clock_timestamp(),
  unique (org_id, as_at, account_label)
);


-- ───────────────────────────────── people: payroll, at your own rates

create table public.employees (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  location_id uuid references public.locations (id),
  employee_no text not null,
  name        text not null,
  department  text not null default '',
  basic_monthly      numeric(18,2) not null default 0 check (basic_monthly >= 0),
  allowances_monthly numeric(18,2) not null default 0 check (allowances_monthly >= 0),
  medical_monthly    numeric(18,2) not null default 0 check (medical_monthly >= 0),
  employer_pf_rate   numeric(6,4) not null default 0 check (employer_pf_rate between 0 and 1),
  employee_pf_rate   numeric(6,4) not null default 0 check (employee_pf_rate between 0 and 1),
  joined      date,
  left_on     date,
  eobi_registered boolean not null default true,
  social_security boolean not null default false,
  is_active   boolean not null default true,
  created_at  timestamptz not null default clock_timestamp(),
  unique (org_id, employee_no)
);


-- ────────────────────────── intelligence: the pipeline and what it costs

create table public.opportunities (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  customer_id uuid references public.customers (id) on delete set null,
  location_id uuid references public.locations (id),
  reference   text not null,
  stage       text not null check (stage in (
                'lead','qualified','quoted','tender_submitted','won','lost')),
  value       numeric(18,2) not null default 0 check (value >= 0),
  expected_close date,
  -- Which item and how many, so the pipeline can be checked against
  -- the warehouse. An opportunity closing in six weeks for a line with
  -- eleven weeks of lead time is a disappointment with a date on it.
  item_id     uuid references public.items (id) on delete set null,
  quantity    numeric(18,4) not null default 0,
  source      text not null default '',
  owner_name  text not null default '',
  opened      date,
  closed      date,
  note        text,
  created_at  timestamptz not null default clock_timestamp(),
  unique (org_id, reference)
);

create index opportunities_open_idx on public.opportunities (org_id, stage)
  where stage in ('lead','qualified','quoted','tender_submitted');

create table public.marketing_spend (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  label       text not null,
  category    text not null default 'other',
  spent_on    date not null,
  amount      numeric(18,2) not null check (amount >= 0),
  created_at  timestamptz not null default clock_timestamp()
);


-- ───────────────────────────── customers: every dealing, written down

-- The thin half of a CRM, and deliberately only the thin half. Stages
-- and forecasts live in opportunities above; what a salesperson
-- actually reaches for is the last thing anybody said to this
-- customer, attached to the record it was about.
create table public.contacts (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  customer_id uuid references public.customers (id) on delete cascade,
  supplier_id uuid references public.suppliers (id) on delete cascade,
  name        text not null,
  role_title  text not null default '',
  email       text,
  phone       text,
  whatsapp    text,
  is_primary  boolean not null default false,
  note        text,
  created_at  timestamptz not null default clock_timestamp(),
  -- A contact belongs to exactly one of the two, never both and never
  -- neither, or "whose contact is this" has no answer.
  constraint contacts_one_owner check (
    (customer_id is not null and supplier_id is null)
    or (customer_id is null and supplier_id is not null))
);

create table public.interactions (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  customer_id uuid references public.customers (id) on delete cascade,
  supplier_id uuid references public.suppliers (id) on delete cascade,
  contact_id  uuid references public.contacts (id) on delete set null,
  -- What it was about. Any of these may be null; an interaction can be
  -- a phone call about nothing in particular.
  machine_id  uuid references public.machines (id) on delete set null,
  invoice_id  uuid references public.invoices (id) on delete set null,
  opportunity_id uuid references public.opportunities (id) on delete set null,
  happened_on date not null,
  channel     text not null default 'note' check (channel in (
                'note','call','visit','email','whatsapp','meeting')),
  summary     text not null,
  next_step   text,
  next_step_on date,
  created_by  uuid references auth.users (id),
  created_at  timestamptz not null default clock_timestamp()
);

create index interactions_customer_idx
  on public.interactions (org_id, customer_id, happened_on desc);


-- ──────────────────────────────────────── invites: the second person in

-- An organisation has had exactly one member since migration 0001:
-- whoever created it. memberships has carried role, branch scope and
-- an invited_by column this whole time with no way to write a second
-- row.
--
-- The invite is keyed on the email rather than on a user id because
-- the person being invited usually has no account yet. It is accepted
-- by a trigger on sign-up, which is the only moment the email and the
-- new user id are both known, and accepting is what writes the
-- membership -- so a half-accepted invite cannot leave someone with a
-- login and no organisation.
create table public.invites (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id) on delete cascade,
  email       text not null,
  role        text not null check (role in ('owner','manager','staff','accountant')),
  location_ids uuid[] not null default '{}',
  -- Both user references clear rather than block. Held as a plain
  -- reference, an invite pins its sender in auth.users forever: deleting
  -- that account fails on a foreign key, which in Supabase surfaces as a
  -- flat refusal to delete a user with no explanation of why. The
  -- attribution is still enforced where it belongs -- the insert policy
  -- below requires invited_by to be the person making the request -- so
  -- nullable here costs an answer to "who invited them" after that
  -- account is gone, and nothing else.
  invited_by  uuid references auth.users (id) on delete set null,
  -- Not a status column: three timestamps say when each thing
  -- happened, and a status column would have to be kept in step with
  -- all three.
  created_at  timestamptz not null default clock_timestamp(),
  expires_at  timestamptz not null default clock_timestamp() + interval '14 days',
  accepted_at timestamptz,
  accepted_by uuid references auth.users (id) on delete set null,
  revoked_at  timestamptz,
  -- Emails are compared case-insensitively everywhere below, so the
  -- stored form is folded on the way in and the uniqueness is real.
  constraint invites_email_lower check (email = lower(email))
);

-- One live invite per email per organisation. Expired, revoked and
-- accepted ones do not count, so a lapsed invite can be reissued.
create unique index invites_live_idx on public.invites (org_id, email)
  where accepted_at is null and revoked_at is null;

create index invites_email_idx on public.invites (email)
  where accepted_at is null and revoked_at is null;


-- Accept every live invite for this address the moment the account
-- exists. Runs as the definer because the new user has no membership
-- yet and therefore cannot see the invite row under the policies
-- below -- the whole point of the invite is that it grants the access
-- it is read with.
create or replace function app.accept_invites()
returns trigger
language plpgsql
security definer
set search_path = public, pg_catalog
as $$
begin
  insert into public.memberships (org_id, user_id, role, location_ids, invited_by)
  select i.org_id, new.id, i.role, i.location_ids, i.invited_by
    from public.invites i
   where i.email = lower(new.email)
     and i.accepted_at is null
     and i.revoked_at is null
     and i.expires_at > clock_timestamp()
  on conflict (org_id, user_id) do nothing;

  update public.invites
     set accepted_at = clock_timestamp(), accepted_by = new.id
   where email = lower(new.email)
     and accepted_at is null
     and revoked_at is null
     and expires_at > clock_timestamp();

  return new;
end;
$$;

create trigger accept_invites_on_signup
  after insert on auth.users
  for each row execute function app.accept_invites();


-- ───────────────────────────────────────────── what a new org may use

-- Migration 0001 put five modules on trial for a new organisation:
-- stock, buy, freight, demand and cash. That was right when those were
-- the only five. Eight more exist now, two of which -- equipment and
-- service -- already had screens in the app and no entitlement to
-- license them, so they worked only because the API was not checking.
-- Checking them against a five-module trial would have locked two
-- working screens on every account.
--
-- So the wedge is every module, still as a trial. The honest reason is
-- that billing does not exist: an entitlement row today records what
-- somebody may use, not what they have bought, and withholding
-- modules nobody is charged for would be a paywall with no payment
-- behind it. When billing arrives this list is where it narrows, and
-- narrowing it is a delete per module rather than a migration.
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
      'customers','books','people','intelligence','equipment','service'
    ]) as m
  on conflict (org_id, module) do nothing;

  return new;
end;
$$;

-- Organisations that already exist got the five. Without this they
-- would keep them, and the account testing the product would see
-- eight locked doors.
insert into public.entitlements (org_id, module, status)
select o.id, m, 'trial'
  from public.organisations o
  cross join unnest(array[
    'sell','money','customers','books','people','intelligence',
    'equipment','service'
  ]) as m
on conflict (org_id, module) do nothing;


-- ────────────────────────────────────────────────────────────── policies

alter table public.accounts          enable row level security;
alter table public.equity_movements  enable row level security;
alter table public.commitments       enable row level security;
alter table public.cash_balances     enable row level security;
alter table public.employees         enable row level security;
alter table public.opportunities     enable row level security;
alter table public.marketing_spend   enable row level security;
alter table public.contacts          enable row level security;
alter table public.interactions      enable row level security;
alter table public.invites           enable row level security;

-- The books are the accountant's and the owner's. A manager running a
-- branch has no business reading the company's equity.
create policy acct_read on public.accounts for select
  using (org_id in (select app.org_ids())
         and org_id in (select app.orgs_with_module('books'))
         and (select app.has_role(org_id, array['owner','accountant'])));
create policy acct_write on public.accounts for insert
  with check ((select app.has_role(org_id, array['owner','accountant']))
              and (select app.has_module(org_id, 'books')));
create policy acct_update on public.accounts for update
  using ((select app.has_role(org_id, array['owner','accountant'])))
  with check ((select app.has_role(org_id, array['owner','accountant'])));
create policy acct_delete on public.accounts for delete
  using ((select app.has_role(org_id, array['owner','accountant'])));

create policy eqm_read on public.equity_movements for select
  using (org_id in (select app.org_ids())
         and (select app.has_role(org_id, array['owner','accountant'])));
create policy eqm_write on public.equity_movements for insert
  with check ((select app.has_role(org_id, array['owner','accountant'])));
create policy eqm_update on public.equity_movements for update
  using ((select app.has_role(org_id, array['owner','accountant'])))
  with check ((select app.has_role(org_id, array['owner','accountant'])));

-- Payroll is the most sensitive table here: one query reveals what
-- every colleague earns. Owner and accountant only, and no branch
-- manager however many locations they run.
create policy emp_read on public.employees for select
  using (org_id in (select app.org_ids())
         and org_id in (select app.orgs_with_module('people'))
         and (select app.has_role(org_id, array['owner','accountant'])));
create policy emp_write on public.employees for insert
  with check ((select app.has_role(org_id, array['owner','accountant']))
              and (select app.has_module(org_id, 'people')));
create policy emp_update on public.employees for update
  using ((select app.has_role(org_id, array['owner','accountant'])))
  with check ((select app.has_role(org_id, array['owner','accountant'])));

create policy comm_read on public.commitments for select
  using (org_id in (select app.org_ids())
         and org_id in (select app.orgs_with_module('cash'))
         and (select app.has_role(org_id, array['owner','manager','accountant'])));
create policy comm_write on public.commitments for insert
  with check ((select app.has_role(org_id, array['owner','manager','accountant']))
              and (select app.has_module(org_id, 'cash')));
create policy comm_update on public.commitments for update
  using ((select app.has_role(org_id, array['owner','manager','accountant'])))
  with check ((select app.has_role(org_id, array['owner','manager','accountant'])));
create policy comm_delete on public.commitments for delete
  using ((select app.has_role(org_id, array['owner','manager'])));

create policy bal_read on public.cash_balances for select
  using (org_id in (select app.org_ids())
         and org_id in (select app.orgs_with_module('cash'))
         and (select app.has_role(org_id, array['owner','manager','accountant'])));
create policy bal_write on public.cash_balances for insert
  with check ((select app.has_role(org_id, array['owner','manager','accountant']))
              and (select app.has_module(org_id, 'cash')));
create policy bal_update on public.cash_balances for update
  using ((select app.has_role(org_id, array['owner','manager','accountant'])))
  with check ((select app.has_role(org_id, array['owner','manager','accountant'])));

create policy opp_read on public.opportunities for select
  using (org_id in (select app.org_ids())
         and org_id in (select app.orgs_with_module('intelligence')));
create policy opp_write on public.opportunities for insert
  with check ((select app.has_role(org_id, array['owner','manager','staff']))
              and (select app.has_module(org_id, 'intelligence')));
create policy opp_update on public.opportunities for update
  using ((select app.has_role(org_id, array['owner','manager','staff'])))
  with check ((select app.has_role(org_id, array['owner','manager','staff'])));

create policy spend_read on public.marketing_spend for select
  using (org_id in (select app.org_ids())
         and org_id in (select app.orgs_with_module('intelligence'))
         and (select app.has_role(org_id, array['owner','manager','accountant'])));
create policy spend_write on public.marketing_spend for insert
  with check ((select app.has_role(org_id, array['owner','manager']))
              and (select app.has_module(org_id, 'intelligence')));
create policy spend_update on public.marketing_spend for update
  using ((select app.has_role(org_id, array['owner','manager'])))
  with check ((select app.has_role(org_id, array['owner','manager'])));

create policy cont_read on public.contacts for select
  using (org_id in (select app.org_ids()));
create policy cont_write on public.contacts for insert
  with check ((select app.has_role(org_id, array['owner','manager','staff'])));
create policy cont_update on public.contacts for update
  using ((select app.has_role(org_id, array['owner','manager','staff'])))
  with check ((select app.has_role(org_id, array['owner','manager','staff'])));
create policy cont_delete on public.contacts for delete
  using ((select app.has_role(org_id, array['owner','manager'])));

create policy intx_read on public.interactions for select
  using (org_id in (select app.org_ids()));
create policy intx_write on public.interactions for insert
  with check ((select app.has_role(org_id, array['owner','manager','staff'])));
-- A written record of what was said is evidence. Correcting a typo is
-- fair; rewriting somebody else's note is not, so an update is the
-- author's own or an owner's.
create policy intx_update on public.interactions for update
  using (created_by = auth.uid()
         or (select app.has_role(org_id, array['owner'])))
  with check (created_by = auth.uid()
              or (select app.has_role(org_id, array['owner'])));

-- An invite is readable by the organisation's managers, and by the
-- person it was sent to -- matched on the email in their own JWT, so
-- nobody can list invites sent to anyone else.
create policy inv_read on public.invites for select
  using ((select app.has_role(org_id, array['owner','manager']))
         or email = lower(coalesce(auth.jwt() ->> 'email', '')));
create policy inv_write on public.invites for insert
  with check ((select app.has_role(org_id, array['owner','manager']))
              and invited_by = auth.uid());
-- Revoking is the only update, and the policy cannot express "only the
-- revoked_at column", so the role gate is the guard and the API sets
-- nothing else.
create policy inv_revoke on public.invites for update
  using ((select app.has_role(org_id, array['owner','manager'])))
  with check ((select app.has_role(org_id, array['owner','manager'])));

-- Migration 0001 already policies this table, and its read and insert
-- rules are right. The update and delete rules are replaced rather than
-- added to, because as written an owner can change or delete their own
-- membership -- and the only owner of an organisation doing that locks
-- themselves out of the thing they created, permanently and silently,
-- with no one left who can let them back in.
--
-- So an owner may act on anyone's membership but their own. That makes
-- "leave this organisation" impossible too, which is the honest cost:
-- leaving needs a check that another owner remains, and that check
-- belongs in a feature that exists, not in a policy written in advance
-- of it.
drop policy mem_update on public.memberships;
drop policy mem_delete on public.memberships;

create policy mem_update on public.memberships for update
  using ((select app.has_role(org_id, array['owner']))
         and user_id <> (select auth.uid()))
  with check ((select app.has_role(org_id, array['owner'])));
create policy mem_delete on public.memberships for delete
  using ((select app.has_role(org_id, array['owner']))
         and user_id <> (select auth.uid()));

commit;
