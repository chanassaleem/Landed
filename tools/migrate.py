#!/usr/bin/env python3
"""Apply the migrations in supabase/migrations, once each, in order.

Why this exists rather than running them on startup.

Applying migrations when a web worker boots means every worker races
every other to run the same DDL, and a platform that starts three
workers runs each migration three times. It also means a schema change
happens at the moment of a deploy with nobody watching, and a failure
halfway through leaves the database between two versions while the new
code -- which needs the finished schema -- is already serving requests.
So: a step somebody runs, that says what it did, and that can be asked
afterwards what state the database is in.

Four commands:

  status    what is applied, what is pending, what has been edited
            since it was applied
  apply     apply the pending ones, in order, stopping at the first
            failure
  verify    exit non-zero if anything is pending or has been edited;
            the gate to run before starting the application
  baseline  record migrations as applied without running them, for a
            database that was brought up to date by hand

Each file is recorded with a checksum of its text. A migration edited
after it was applied is the quiet way a staging database and a
production one come to hold different schemas while both claim to be
at the same version, so it is reported as its own state rather than
being passed over as already done.

Each migration runs in one transaction, owned here, with the record of
it written inside the same one. Ten of the files open and close a
transaction of their own; those keywords are stripped before the file
is run, so the runner's is the only transaction and the schema change
and its bookkeeping cannot come apart. The files are left alone, since
they are already applied in production and their checksums recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import re
import sys

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - depends on the image
    sys.exit("psycopg is not installed. pip install -r requirements.txt")

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "supabase" / "migrations"

# The migration files name themselves 0001_, 0002_ and so on. The number
# is the order, so a file that does not carry one has no place in the
# sequence and is refused rather than guessed at.
NUMBERED = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

# A migration's own BEGIN or COMMIT, on a line of its own. Removed
# before the file is run so the runner's transaction is the only one,
# and the record of the migration commits with it.
TRANSACTION_KEYWORD = re.compile(
    r"(?mi)^[ \t]*(?:begin|commit)[ \t]*;[ \t]*$\n?")

LEDGER = """
create table if not exists public.schema_migrations (
  version     text primary key,
  filename    text not null,
  checksum    text not null,
  applied_at  timestamptz not null default now(),
  applied_by  text not null default current_user,
  -- A migration recorded by `baseline` was not run by this tool: the
  -- schema was brought up to date some other way. Worth being able to
  -- tell apart when a figure does not match what the file says.
  baselined   boolean not null default false
);

comment on table public.schema_migrations is
  'One row per applied migration. The checksum is of the file text, so '
  'a migration edited after it was applied can be told from one that '
  'was never applied -- which is how two databases come to hold '
  'different schemas while both claim the same version.';
"""


def _files() -> list[tuple[str, pathlib.Path]]:
    found, seen = [], {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        m = NUMBERED.match(path.name)
        if not m:
            sys.exit(
                f"{path.name} is not named like a migration. Expected "
                f"0000_lower_case_words.sql -- the number is the order, "
                f"and a file without one has no place in the sequence."
            )
        version = m.group(1)
        if version in seen:
            sys.exit(
                f"Two migrations are numbered {version}: {seen[version]} "
                f"and {path.name}. One of them has to be renumbered, or "
                f"which runs first depends on how the filenames sort."
            )
        seen[version] = path.name
        found.append((version, path))
    if not found:
        sys.exit(f"No migrations found in {MIGRATIONS}.")
    return found


def _checksum(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _dsn(given: str | None) -> str:
    dsn = given or os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit(
            "No database. Pass --dsn or set DATABASE_URL.\n"
            "On Supabase this is the session pooler string (Connect -> "
            "Session pooler, port 5432), not the direct connection: the "
            "direct one resolves to IPv6 only without the IPv4 add-on."
        )
    return dsn


def _state(conn) -> list[dict]:
    """Every migration, with what the database says about it."""
    conn.execute(LEDGER)
    applied = {
        r[0]: (r[1], r[2]) for r in conn.execute(
            "select version, checksum, baselined"
            "  from public.schema_migrations").fetchall()
    }
    rows = []
    for version, path in _files():
        now = _checksum(path)
        if version not in applied:
            state = "pending"
        elif applied[version][0] != now:
            state = "edited"
        else:
            state = "applied"
        rows.append({"version": version, "file": path.name, "path": path,
                     "state": state,
                     "baselined": bool(applied.get(version, (None, False))[1]),
                     "was": applied.get(version, (None,))[0], "now": now})
    # A row in the table with no file is a database ahead of this
    # checkout -- a deploy of older code against a newer database, which
    # is worth saying out loud rather than reporting everything as fine.
    known = {r["version"] for r in rows}
    for version in sorted(set(applied) - known):
        rows.append({"version": version, "file": "(no file here)",
                     "path": None, "state": "unknown", "baselined": False,
                     "was": applied[version][0], "now": None})
    return sorted(rows, key=lambda r: r["version"])


# ------------------------------------------------------------- commands


def cmd_status(args) -> int:
    with psycopg.connect(_dsn(args.dsn), autocommit=True) as conn:
        rows = _state(conn)
    width = max(len(r["file"]) for r in rows)
    for r in rows:
        mark = {"applied": "ok", "pending": "->", "edited": "!!",
                "unknown": "??"}[r["state"]]
        extra = ""
        if r["state"] == "edited":
            extra = f"  (applied as {r['was']}, now {r['now']})"
        elif r["state"] == "unknown":
            extra = "  (applied here, but no such file in this checkout)"
        elif r["baselined"]:
            extra = "  (baselined, not run by this tool)"
        print(f"  {mark} {r['file']:<{width}}  {r['state']}{extra}")

    pending = [r for r in rows if r["state"] == "pending"]
    edited = [r for r in rows if r["state"] == "edited"]
    unknown = [r for r in rows if r["state"] == "unknown"]
    print()
    print(f"  {len(rows) - len(pending) - len(unknown)} applied, "
          f"{len(pending)} pending, {len(edited)} edited, "
          f"{len(unknown)} unknown")
    return 0


def cmd_verify(args) -> int:
    """The gate. Nothing pending, nothing edited, or the deploy stops."""
    with psycopg.connect(_dsn(args.dsn), autocommit=True) as conn:
        rows = _state(conn)
    pending = [r for r in rows if r["state"] == "pending"]
    edited = [r for r in rows if r["state"] == "edited"]
    unknown = [r for r in rows if r["state"] == "unknown"]

    if pending:
        print(f"{len(pending)} migration(s) have not been applied: "
              f"{', '.join(r['file'] for r in pending)}", file=sys.stderr)
    for r in edited:
        print(f"{r['file']} was edited after it was applied "
              f"({r['was']} -> {r['now']}). The database may not hold "
              f"what the file now says.", file=sys.stderr)
    for r in unknown:
        print(f"{r['version']} is applied in this database but is not in "
              f"this checkout. This code may be older than the schema.",
              file=sys.stderr)

    if pending or edited:
        print("\nSchema is not at the version this code expects. Run "
              "`python tools/migrate.py apply` before starting the "
              "application.", file=sys.stderr)
        return 1
    if unknown:
        # Not fatal: a newer schema with older code usually works, and
        # refusing to start would turn a rollback into an outage.
        print("\nSchema is ahead of this checkout. Starting anyway.",
              file=sys.stderr)
    print(f"Schema is up to date ({len(rows) - len(unknown)} migrations).")
    return 0


def cmd_apply(args) -> int:
    dsn = _dsn(args.dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = _state(conn)
        edited = [r for r in rows if r["state"] == "edited"]
        if edited and not args.force:
            for r in edited:
                print(f"{r['file']} was edited after it was applied "
                      f"({r['was']} -> {r['now']}).", file=sys.stderr)
            print("\nRe-running it is not the fix: it has already run "
                  "once, and most of these are not written to run twice. "
                  "Write a new migration for the change, or pass --force "
                  "to record the new checksum without running anything.",
                  file=sys.stderr)
            return 1
        if edited and args.force:
            for r in edited:
                conn.execute(
                    "update public.schema_migrations set checksum = %s"
                    " where version = %s", (r["now"], r["version"]))
                print(f"  recorded new checksum for {r['file']}")

        pending = [r for r in rows if r["state"] == "pending"]
        if not pending:
            print("Nothing to apply.")
            return 0

        for r in pending:
            text = r["path"].read_text()
            # One transaction per migration, owned here, with the
            # record of it written inside the same one.
            #
            # Ten of these files open and close their own. Running
            # them as written meant the schema change committed and
            # the tracking row went in afterwards, in a transaction of
            # its own -- so a process killed between the two left a
            # database holding the change with no record of it, and
            # the next run would try to apply it again. A runner whose
            # bookkeeping can come apart from the thing it is keeping
            # books on is not much of a runner.
            #
            # Their BEGIN and COMMIT are stripped at execution rather
            # than edited out of the files, because the files are
            # already applied in production and their checksums are
            # recorded: editing them would make every one report as
            # changed. Each has exactly one pair, and none contains
            # DDL that cannot run inside a transaction -- both checked
            # across all of them.
            body = TRANSACTION_KEYWORD.sub("", text)
            print(f"  applying {r['file']} ... ", end="", flush=True)
            try:
                with conn.transaction():
                    conn.execute(body)
                    conn.execute(
                        "insert into public.schema_migrations"
                        " (version, filename, checksum)"
                        " values (%s,%s,%s)",
                        (r["version"], r["file"], r["now"]))
            except Exception as exc:                 # noqa: BLE001
                print("failed")
                print(f"\n{r['file']} failed:\n  {exc}", file=sys.stderr)
                done = [p["file"] for p in pending
                        if p["version"] < r["version"]]
                at = done[-1][:4] if done else "its previous version"
                print(f"\nApplied in this run: "
                      f"{', '.join(done) if done else 'none'}.\n"
                      f"Not applied: {r['file']} and everything after it.\n"
                      f"Nothing from {r['file']} was left behind -- it ran "
                      f"in a transaction and rolled back whole, so the "
                      f"schema is at {at}.\n"
                      f"Fix the migration and run apply again: it starts "
                      f"from here, not from the beginning.\n"
                      f"See supabase/MIGRATIONS.md for recovery.",
                      file=sys.stderr)
                return 1
            print("ok")
    print(f"\nApplied {len(pending)} migration(s).")
    return 0


def cmd_baseline(args) -> int:
    """Record migrations as applied without running them.

    For the database that was brought up to date by hand before this
    tool existed. Running them again would fail on the first `create
    table`, and that failure would say nothing about which of them had
    really been applied.
    """
    with psycopg.connect(_dsn(args.dsn), autocommit=True) as conn:
        rows = _state(conn)
        through = args.through
        marked = []
        for r in rows:
            if r["state"] != "pending":
                continue
            if through and r["version"] > through:
                continue
            conn.execute(
                "insert into public.schema_migrations (version, filename,"
                " checksum, baselined) values (%s,%s,%s,true)"
                " on conflict (version) do nothing",
                (r["version"], r["file"], r["now"]))
            marked.append(r["file"])
    if not marked:
        print("Nothing to baseline.")
        return 0
    for name in marked:
        print(f"  recorded {name} as already applied")
    print(f"\nBaselined {len(marked)} migration(s). Nothing was run "
          f"against the database.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Apply the migrations in supabase/migrations.")
    ap.add_argument("--dsn", help="defaults to $DATABASE_URL")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="what is applied and what is pending")
    sub.add_parser("verify", help="exit non-zero if not up to date")
    a = sub.add_parser("apply", help="apply the pending migrations")
    a.add_argument("--force", action="store_true",
                   help="record new checksums for edited migrations "
                        "without running anything")
    b = sub.add_parser("baseline",
                       help="record migrations as applied without running")
    b.add_argument("--through", metavar="NNNN",
                   help="stop after this version, e.g. 0015")

    args = ap.parse_args(argv)
    return {"status": cmd_status, "verify": cmd_verify,
            "apply": cmd_apply, "baseline": cmd_baseline}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
