# Landed

Working-capital software for importers. What to reorder, when, and
whether you can afford it — with the cost of the goods counted properly,
including the freight, duty and clearing that a supplier invoice does not
show you.

Built first for medical device importers in Pakistan, on a schema that
assumes nothing about the country.

## Why it is called Landed

An imported item's real cost is not the supplier's price. It is that plus
freight, duty, clearing, inland transport and bank charges, divided
across the shipment on the right basis for each. Get it wrong and every
figure downstream is wrong with it: unit cost feeds annual consumption
value, which feeds the ABC class, which feeds the service target, which
feeds safety stock. Most software at this price point does not try.

## How it is put together

```
engine/      the calculations. Pure functions, no state, no I/O.
web/         FastAPI app, serving its own UI. One service, one deploy.
supabase/
  migrations/  the schema and every row-level security policy
  tests/       a shim of Supabase's auth schema, so CI can run the policies
tests/       589 engine tests + the policy and schema suites
tools/       the Excel model generator and the model verifier
Dockerfile   Cloud Run
```

The engine came over from `sop-insight` unchanged, and its tests pass
here untouched — that was the condition for starting this repository at
all. It reads a snapshot and returns results; it writes nothing and
knows nothing about Supabase, which is why none of it had to change.

### The ledger

The system of record is `stock_movement`: one append-only row for every
change in stock, signed, with the document that caused it. Stock on hand
is not stored anywhere — it is the sum of those rows.

That costs a little speed and buys the property that matters: there is no
second number that can disagree with the ledger, because there is no
second number. It also makes offline sync nearly free. Appends from two
devices never conflict; they just both append. No CRDTs, just a queue and
an idempotency key.

Corrections are reversing entries. The table rejects `update` and
`delete` by trigger rather than by policy, so the rule holds even against
the service role.

## Running it

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q          # engine + schema guards, no database needed
```

The policy tests need a Postgres. Any one will do:

```bash
createdb landed
psql -d landed -f supabase/tests/00_supabase_shim.sql
for f in supabase/migrations/*.sql; do psql -d landed -v ON_ERROR_STOP=1 -f "$f"; done

LANDED_TEST_DSN='postgres:///landed' python -m pytest tests/test_rls.py -q
```

CI runs both on every push, against a real Postgres 16.

## Security, in three rules

1. **Every tenant table carries `org_id`, and every policy starts with the
   same wall.** `tests/test_schema_guards.py` fails the build if a table
   is added without RLS, without a policy, or without `org_id`.

2. **Never `for all` on a write policy.** A `FOR ALL` policy's `USING`
   clause also governs `SELECT`, and permissive policies are OR'd — so
   one of them silently re-opens read access past the module gate and the
   branch scoping. This was in the first draft of nearly every table
   here; there is now a test for it.

3. **Never read tenancy from `user_metadata`.** It is writable by the
   user, so a tenant id taken from it is not a boundary. Helpers live in
   the `app` schema, which PostgREST does not expose, and every
   `SECURITY DEFINER` function pins its `search_path`.

The service role key bypasses all of it. Server-side only, never in a
browser bundle.

## Deploying

Cloud Run, because it scales to zero and an idle API then costs nothing:

```bash
gcloud run deploy landed --source . --region australia-southeast1 \
    --allow-unauthenticated
```

**Put Cloud Run in the same region as the Supabase project.** That is the
decision that matters, more than which region either of them is in. The
app makes many queries per page and the user makes one request per page,
so the chatty half should happen inside one datacentre and the slow half
should happen once. The project currently lives in Sydney, hence
`australia-southeast1`.

If the browser ever starts calling Supabase directly through
`supabase-js`, that reasoning inverts and the database's distance from
the customer becomes the thing that hurts. Mumbai (`asia-south1`) is the
closest region to Pakistan if it comes to that — but a Supabase project
cannot change region in place. You create a new project and migrate,
which also changes the database host, the project URL, the API keys and
the auth callback URLs. Cheap while the database is empty; a cutover
once a customer's ledger is in it.

Render's free tier also works, but its web services sleep after 15
minutes and take 30–60 seconds to wake, which is a bad minute to spend
standing in a customer's office.

## Status

Phase 1 of five. The spine is in: tenancy, roles, branch scoping, module
entitlements, the ledger, goods receipt, and landed cost — all with
policies that are tested rather than assumed. Next is the importer
screens on top of it.
