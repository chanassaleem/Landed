-- The parts of Supabase a migration depends on, so the schema can be
-- applied and its policies tested on a plain Postgres in CI.
-- Supabase creates these itself; this file exists only for the test
-- harness and is never applied to a real project.

create schema if not exists auth;

create table if not exists auth.users (
  id    uuid primary key default gen_random_uuid(),
  email text unique
);

-- Supabase reads the signed JWT; here the test sets request.jwt.claims
-- directly, which is the same mechanism underneath.
-- The nullif goes around the RAW setting, before the cast, which is how
-- Supabase itself writes it. The reason is not obvious: when a SET LOCAL
-- transaction ends, the setting does not become unset, it becomes the
-- empty string -- and ''::jsonb raises "invalid input syntax for type
-- json". So on a POOLED connection, the first unauthenticated query
-- after any authenticated one fails, which looks like a random
-- intermittent error and is really this.
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
  select nullif(
    nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub',
    ''
  )::uuid
$$;

create or replace function auth.jwt()
returns jsonb
language sql
stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claims', true), '')::jsonb,
    '{}'::jsonb
  )
$$;

do $$ begin
  create role anon nologin;
exception when duplicate_object then null; end $$;
do $$ begin
  create role authenticated nologin;
exception when duplicate_object then null; end $$;
do $$ begin
  create role service_role nologin bypassrls;
exception when duplicate_object then null; end $$;

-- Supabase grants these itself; without them a policy that calls
-- auth.uid() fails with "permission denied for schema auth", which shows
-- up as a confusing row-level-security violation rather than as itself.
grant usage on schema auth to anon, authenticated, service_role;
grant execute on function auth.uid(), auth.jwt() to anon, authenticated, service_role;

grant usage on schema public to anon, authenticated, service_role;
alter default privileges in schema public
  grant all on tables to authenticated, service_role;
