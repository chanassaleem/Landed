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

## Connecting it to Supabase

Everything the app reads goes through `engine/db/supabase.py`, and it
will only hand out a scoped session. `as_user(id)` sets the role and the
claims the policies read; `as_service()` does not, and says so in its
name. A plain `psycopg.connect` with the database password is a
superuser connection that bypasses every policy in `supabase/migrations`
while reading exactly like the safe version, which is why there is a
module instead of a connection string in a route.

Set one environment variable on the host:

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | the **Session pooler** string from Supabase → Connect |

Use the Session pooler, not the direct connection. The direct string
resolves to IPv6 only unless the project pays for the IPv4 add-on, and
most hosts are IPv4-only; the failure is a name-resolution error that
looks nothing like its cause. `/health/supabase` warns if it sees the
wrong one.

Then ask the running service whether it worked:

```bash
curl -s https://<your-app>/health/supabase | jq
{
  "ok": true,
  "reachable": true,
  "rls_holding": true,
  "latency_ms": 214,
  "detail": "Connected, and a user with no membership sees nothing."
}
```

`rls_holding` is the half worth having. A Postgres with row-level
security switched off answers every ordinary health check perfectly
while serving every tenant's rows to anyone who asks, so the probe reads
a protected table as a user who belongs to no organisation and expects
to be told nothing. The endpoint returns 503 when either half fails, so
a platform health check and a person reading the JSON agree.

## Switching sign-in on

Two environment variables, and two settings in the Supabase dashboard.
None of it is guessable from the code, which is why it is written down.

**Environment** (Render, or `.env` locally):

    SUPABASE_URL=https://<project-ref>.supabase.co
    SUPABASE_PUBLISHABLE_KEY=sb_publishable_...

The publishable key replaced the anon key and carries the same low
privileges, so the policies in `supabase/migrations` behave the same.
`SUPABASE_ANON_KEY` still works for a project that has not migrated.
The secret key is not used and should not be set: it runs as a role
that bypasses every policy in the schema.

With neither set, the app opens on sample data and says on screen that
sign-in is off. `/health/auth` names what is missing.

**Authentication → URL Configuration:**

- Site URL: the deployed address, e.g. `https://landed-wvsm.onrender.com`.
  It defaults to `http://localhost:3000`, so until it is changed every
  confirmation email points at a machine that is not running.
- Redirect URLs: add `https://<your-host>/auth/confirm`.

**Authentication → Email Templates → Confirm signup**, change the link to:

    <a href="{{ .SiteURL }}/auth/confirm?token_hash={{ .TokenHash }}&type=signup">
      Confirm your email address
    </a>

Not the default `{{ .ConfirmationURL }}`. That one confirms at Supabase
and then redirects back with the session in the URL -- in the fragment,
or as a code to exchange -- and a session in a URL is kept: browser
history, the referrer header, a screenshot in a bug report. The code
form also only works in the browser that signed up, so signing up on a
laptop and opening the email on a phone fails.

`{{ .TokenHash }}` is redeemed by `/auth/confirm` on the server. The
browser gets a cookie it cannot read and a plain path.

To skip confirmation entirely while testing, turn off **Confirm email**
under Authentication → Providers → Email. Sign-up then returns a session
immediately. Turn it back on before anyone else has an account.

## Status

Phase 1 of five. The spine is in: tenancy, roles, branch scoping, module
entitlements, the ledger, goods receipt, and landed cost — all with
policies that are tested rather than assumed. Next is the importer
screens on top of it.
