-- Money coming in, as rows rather than as a total that drifts.
--
-- public.invoices.amount_paid was a stored number with nothing
-- behind it. Somebody typed over it, or an import moved it, and
-- afterwards there was no way to answer the questions a collections
-- call actually turns on: when did they pay, how much, against
-- which invoices, and is the 400,000 that arrived on Tuesday the
-- one they are talking about.
--
-- That is the problem the stock ledger exists to avoid, applied to
-- cash. 0003 says it plainly for stock: there is no second number
-- that can disagree with the ledger, because there is no second
-- number. The receivable book had the second number and not the
-- ledger.
--
-- amount_paid stays, and keeps its meaning: the position as it was
-- when the book was imported, the way an 'opening' movement is the
-- stock on the shelf the day the file was loaded. Everything
-- recorded from then on is a payment. Replacing it would have
-- thrown away every importer's opening ledger to gain nothing, and
-- re-importing a workbook would have had nowhere to put the history
-- it carries.

create table public.payments (
  id           uuid primary key default gen_random_uuid(),
  org_id       uuid not null references public.organisations (id)
                    on delete cascade,
  customer_id  uuid not null references public.customers (id)
                    on delete cascade,
  received_on  date not null default current_date,
  amount       numeric(18,2) not null check (amount > 0),
  currency     text not null default 'PKR',
  fx_rate      numeric(18,6) not null default 1,
  -- How it arrived, because "did the cheque clear" and "did the
  -- transfer land" are different follow-ups.
  method       text check (method in ('cash', 'cheque', 'transfer',
                                      'card', 'other')),
  reference    text,
  note         text,
  -- A person pressing the button twice, or a connection dropping
  -- after the write and before the answer. Same mechanism the
  -- ledger operations use.
  client_ref   text,
  created_at   timestamptz not null default now(),
  created_by   uuid references auth.users (id) default auth.uid()
);

-- A refund is its own thing, not a payment typed with a minus in
-- front of it, which is why amount is checked above rather than
-- left signed.
comment on table public.payments is
  'One row per payment received. The paid position of an invoice is '
  'its imported amount_paid plus what is allocated to it here -- see '
  'public.invoice_settlement.';

create unique index payments_once_idx
  on public.payments (org_id, client_ref)
  where client_ref is not null;

create index payments_customer_idx
  on public.payments (org_id, customer_id, received_on desc);

-- What a payment settles. Separate from the payment because a
-- customer pays a round number against three invoices and a credit
-- note, and a payment that cannot be split is one somebody records
-- against the wrong invoice to make the screen balance.
create table public.payment_allocations (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.organisations (id)
                   on delete cascade,
  payment_id  uuid not null references public.payments (id)
                   on delete cascade,
  invoice_id  uuid not null references public.invoices (id)
                   on delete cascade,
  amount      numeric(18,2) not null check (amount > 0),
  created_at  timestamptz not null default now()
);

create index payment_allocations_invoice_idx
  on public.payment_allocations (org_id, invoice_id);
create index payment_allocations_payment_idx
  on public.payment_allocations (org_id, payment_id);

alter table public.payments            enable row level security;
alter table public.payment_allocations enable row level security;

-- Split by command, never `for all`. A FOR ALL policy's USING
-- clause also governs SELECT, and permissive policies are OR'd
-- together -- see the note in 0002.
create policy pay_read on public.payments for select
  using (org_id in (select app.org_ids())
         and (select app.has_module(org_id, 'money')));
create policy pay_insert on public.payments for insert
  with check (org_id in (select app.org_ids())
              and (select app.has_role(org_id,
                     array['owner', 'accountant', 'manager'])));
-- No update and no delete. A payment recorded wrongly is reversed
-- by its own record, not edited away, for the reason the ledger
-- gives: history that can be rewritten is not history.
create policy pay_update_none on public.payments for update
  using (false);

create policy pal_read on public.payment_allocations for select
  using (org_id in (select app.org_ids())
         and (select app.has_module(org_id, 'money')));
create policy pal_insert on public.payment_allocations for insert
  with check (org_id in (select app.org_ids())
              and (select app.has_role(org_id,
                     array['owner', 'accountant', 'manager'])));
create policy pal_delete on public.payment_allocations for delete
  using (org_id in (select app.org_ids())
         and (select app.has_role(org_id, array['owner', 'accountant'])));

-- An invoice, with what has actually been settled against it.
--
-- Every module reads this rather than invoices.amount_paid, so a
-- payment taken on the phone shows on the next refresh instead of
-- at the next import.
create view public.invoice_settlement
with (security_invoker = true) as
  with allocated as (
    select a.org_id, a.invoice_id, sum(a.amount) as amount
      from public.payment_allocations a
     group by a.org_id, a.invoice_id
  )
  select i.org_id,
         i.id,
         i.invoice_no,
         i.customer_id,
         i.location_id,
         i.invoice_date,
         i.due_date,
         i.amount,
         i.currency,
         -- The opening position plus everything settled since.
         i.amount_paid + coalesce(al.amount, 0)          as paid,
         i.amount - i.amount_paid - coalesce(al.amount, 0)
                                                         as outstanding,
         i.amount_paid                                   as opening_paid,
         coalesce(al.amount, 0)                          as settled_here
    from public.invoices i
    left join allocated al
      on al.invoice_id = i.id and al.org_id = i.org_id;

comment on view public.invoice_settlement is
  'An invoice with what has actually been settled against it: the '
  'amount_paid the book was imported with, plus every allocation '
  'recorded since. outstanding is what to chase.';

-- What a customer owes, and what they have paid that is not against
-- anything yet. Money on account is real -- a customer pays 500,000
-- against 400,000 of invoices because that is what the cheque was
-- for -- and a book that cannot hold it loses the 100,000 or
-- allocates it somewhere wrong.
create view public.customer_account
with (security_invoker = true) as
  with owed as (
    select org_id, customer_id, sum(outstanding) as outstanding
      from public.invoice_settlement
     group by org_id, customer_id
  ),
  taken as (
    select p.org_id, p.customer_id,
           sum(p.amount)                         as received,
           sum(p.amount) - coalesce(sum(a.amount), 0) as unallocated
      from public.payments p
      left join (
        select payment_id, sum(amount) as amount
          from public.payment_allocations group by payment_id
      ) a on a.payment_id = p.id
     group by p.org_id, p.customer_id
  )
  select c.org_id,
         c.id            as customer_id,
         c.code,
         c.name,
         coalesce(o.outstanding, 0)  as outstanding,
         coalesce(t.received, 0)     as received,
         coalesce(t.unallocated, 0)  as unallocated
    from public.customers c
    left join owed  o on o.customer_id = c.id and o.org_id = c.org_id
    left join taken t on t.customer_id = c.id and t.org_id = c.org_id;

comment on view public.customer_account is
  'Per customer: what is still outstanding, what has been received, '
  'and how much of it is not allocated to an invoice yet. The last '
  'is money on account, which is ordinary rather than an error.';
