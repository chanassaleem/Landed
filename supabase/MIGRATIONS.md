# Migrations

Every schema change is a numbered file in `supabase/migrations`. They
run once each, in order, and `tools/migrate.py` is what runs them.

## Why not on startup

Applying migrations when a web worker boots means every worker races
every other to run the same DDL, and a platform that starts three
workers runs each migration three times. It also means a schema change
happens during a deploy with nobody watching, and a failure halfway
through leaves the database between two versions while the new code --
which needs the finished schema -- is already serving requests.

So migrating is a step somebody runs, and starting the application is
gated on the schema already being right.

## The four commands

```
python tools/migrate.py status     # what is applied, pending, edited
python tools/migrate.py apply      # apply the pending ones, in order
python tools/migrate.py verify     # exit non-zero if not up to date
python tools/migrate.py baseline   # record as applied without running
```

All of them take `--dsn`, and default to `$DATABASE_URL`.

On Supabase that must be the **session pooler** string (Connect →
Session pooler, port 5432). The direct connection string resolves to
IPv6 only unless the project pays for the IPv4 add-on, and the failure
looks like a name-resolution error rather than anything to do with
networking stacks.

## Deploying

The container runs `verify` before `uvicorn`. If anything is pending or
has been edited, the process exits non-zero and never starts serving --
so a deploy whose migrations have not been applied fails as a deploy
rather than coming up and returning errors that look like application
bugs.

The order for a release that includes a schema change:

1. `python tools/migrate.py apply` against production.
2. Deploy the application.

Not the other way round. New code against an old schema fails on the
first query that needs the new column; old code against a new schema
almost always works, which is what makes a rollback safe.

To check production from anywhere, without deploying anything:

```
DATABASE_URL='...' python tools/migrate.py status
```

## A database that was migrated by hand

Running the files again would fail on the first `create table`, and
that failure would say nothing about which of them had really been
applied. `baseline` records them as applied without running anything:

```
python tools/migrate.py baseline --through 0015
python tools/migrate.py status          # check it looks right
python tools/migrate.py apply           # 0016 onwards
```

Baselined rows are marked as such in `public.schema_migrations`, so it
stays possible to tell a migration this tool ran from one it was told
had already happened.

## When one fails

Each migration runs in a transaction -- its own, where the file opens
one, and one wrapped around it where the file does not. A failure rolls
the whole file back, so the schema is left at the last migration that
succeeded and nothing is recorded as applied that is not.

`apply` stops at the first failure and does not attempt the ones after
it. Fix the file and run `apply` again: it starts from the one that
failed, not from the beginning.

The two things not to do:

- **Do not edit a migration that has already been applied.** The
  checksum will not match and `verify` will refuse to start the
  application, which is the point: a migration edited after it was
  applied is how a staging database and a production one come to hold
  different schemas while both claim to be at the same version. Write a
  new migration instead.
- **Do not delete rows from `public.schema_migrations`** to make
  something run again. Most of these files are not written to run
  twice.

If a migration has already been applied and the file genuinely needs to
change -- a comment, a typo in a comment -- `apply --force` records the
new checksum without running anything.

## Rolling back

There are no down-migrations, deliberately. A down-migration for
anything that drops or rewrites data is a second piece of untested code
that runs at the worst possible moment, and the ones for additive
changes are not needed.

What to do instead, in order of preference:

1. **Roll back the application, not the schema.** Almost every
   migration here is additive: new columns, new views, new tables. Old
   code ignores them. This is the reason to apply migrations before
   deploying rather than after.
2. **Write a forward migration** that undoes what the last one did.
   Numbered after it, reviewed like any other, and it leaves the
   history showing what actually happened.
3. **Restore from a backup**, if the migration destroyed data. Supabase
   keeps point-in-time recovery on paid plans; check the retention
   window before relying on it.

A migration that cannot be undone by any of those three is one to read
twice before applying -- particularly anything that drops a column or a
table, because the data in it is gone the moment it commits.
