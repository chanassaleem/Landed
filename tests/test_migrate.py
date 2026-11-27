"""The migration runner.

Applying migrations when a web worker boots means every worker races
every other to run the same DDL, and a platform that starts three
workers runs each migration three times. So migrating is a step
somebody runs, and starting the application is gated on the schema
already being right.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.conninfo import conninfo_to_dict, make_conninfo

DSN = os.environ.get("LANDED_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="LANDED_TEST_DSN not set")

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNNER = ROOT / "tools" / "migrate.py"
SHIM = ROOT / "supabase" / "tests" / "00_supabase_shim.sql"
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))


def run(dsn, *args):
    return subprocess.run(
        [sys.executable, str(RUNNER), "--dsn", dsn, *args],
        capture_output=True, text=True, cwd=ROOT)


@pytest.fixture
def blank():
    """An empty database with only the Supabase shim in it."""
    name = f"mig_{uuid.uuid4().hex[:10]}"
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f'create database "{name}"')
    # Built from the connection's own parts, not by editing its text.
    #
    # This replaced the literal "/landed?" in the DSN, which works on
    # a local socket URL and does nothing at all on CI's
    # postgres://postgres:postgres@localhost:5432/landed -- no
    # question mark, so no match, so every one of these tests ran
    # against the shared database and collided with the tables
    # already in it. Eight failures that said nothing about the
    # migration runner and everything about this line.
    parts = conninfo_to_dict(DSN)
    parts["dbname"] = name
    dsn = make_conninfo(**parts)
    con = psycopg.connect(dsn, autocommit=True)
    con.execute(SHIM.read_text())
    yield dsn, con
    con.close()
    admin.execute(f'drop database "{name}" with (force)')
    admin.close()


def applied(con) -> list[str]:
    return [r[0] for r in con.execute(
        "select version from public.schema_migrations order by version"
    ).fetchall()]


def test_a_fresh_database_migrates_from_zero(blank):
    dsn, con = blank
    out = run(dsn, "apply")
    assert out.returncode == 0, out.stderr
    assert applied(con) == [p.name[:4] for p in MIGRATIONS]
    # And the schema is really there, not just recorded.
    n = con.execute(
        "select count(*) from information_schema.tables"
        " where table_schema = 'public' and table_name = 'stock_movement'"
    ).fetchone()[0]
    assert n == 1


def test_verify_refuses_a_database_that_is_behind(blank):
    """The deploy gate. A deploy whose migrations have not been applied
    should fail as a deploy, not come up and return errors that look
    like application bugs."""
    dsn, _con = blank
    out = run(dsn, "verify")
    assert out.returncode == 1
    assert "have not been applied" in out.stderr
    assert "0001_spine.sql" in out.stderr


def test_verify_passes_once_everything_is_applied(blank):
    dsn, _con = blank
    assert run(dsn, "apply").returncode == 0
    out = run(dsn, "verify")
    assert out.returncode == 0
    assert "up to date" in out.stdout


def test_an_existing_database_applies_only_what_is_missing(blank):
    dsn, con = blank
    for path in MIGRATIONS[:-2]:
        con.execute(path.read_text())
    run(dsn, "baseline", "--through", MIGRATIONS[-3].name[:4])

    out = run(dsn, "apply")
    assert out.returncode == 0, out.stderr
    for path in MIGRATIONS[:-2]:
        assert path.name not in out.stdout, "it re-ran an applied migration"
    for path in MIGRATIONS[-2:]:
        assert path.name in out.stdout
    assert applied(con) == [p.name[:4] for p in MIGRATIONS]


def test_applying_twice_does_nothing_the_second_time(blank):
    dsn, _con = blank
    assert run(dsn, "apply").returncode == 0
    out = run(dsn, "apply")
    assert out.returncode == 0
    assert "Nothing to apply" in out.stdout


def test_a_failing_migration_stops_the_run_and_rolls_back(blank, tmp_path):
    """And leaves nothing behind. A migration half applied is the state
    that makes the next run's failure unreadable."""
    dsn, con = blank
    broken = ROOT / "supabase" / "migrations" / "9998_broken.sql"
    broken.write_text("create table public.before_the_break (id int);\n"
                      "select 1 / 0;\n")
    after = ROOT / "supabase" / "migrations" / "9999_after.sql"
    after.write_text("create table public.never_reached (id int);\n")
    try:
        out = run(dsn, "apply")
        assert out.returncode == 1
        assert "9998_broken.sql failed" in out.stderr
        assert "division by zero" in out.stderr
        # Nothing from the failing file, and nothing after it.
        for table in ("before_the_break", "never_reached"):
            n = con.execute(
                "select count(*) from information_schema.tables"
                " where table_schema='public' and table_name=%s",
                (table,)).fetchone()[0]
            assert n == 0, f"{table} survived a failed migration"
        assert "9998" not in applied(con)
        assert "9999" not in applied(con)
        # The real ones before it did land, so a second run resumes.
        assert "0001" in applied(con)
    finally:
        broken.unlink()
        after.unlink()


def test_a_migration_edited_after_it_was_applied_is_reported(blank,
                                                             monkeypatch):
    """How a staging database and a production one come to hold
    different schemas while both claim to be at the same version."""
    dsn, con = blank
    extra = ROOT / "supabase" / "migrations" / "9997_extra.sql"
    extra.write_text("create table public.extra_one (id int);\n")
    try:
        assert run(dsn, "apply").returncode == 0
        extra.write_text("create table public.extra_one (id int);\n"
                         "-- edited after it was applied\n")

        status = run(dsn, "status")
        assert "edited" in status.stdout

        verify = run(dsn, "verify")
        assert verify.returncode == 1
        assert "was edited after it was applied" in verify.stderr

        # Re-running is not the fix, and it says so rather than doing it.
        apply = run(dsn, "apply")
        assert apply.returncode == 1
        assert "not the fix" in apply.stderr

        forced = run(dsn, "apply", "--force")
        assert forced.returncode == 0
        assert run(dsn, "verify").returncode == 0
    finally:
        extra.unlink()


def test_baseline_records_without_running_anything(blank):
    """For the database brought up to date by hand before this existed.
    Running the files again would fail on the first `create table`, and
    that failure would say nothing about which had really been applied."""
    dsn, con = blank
    out = run(dsn, "baseline")
    assert out.returncode == 0
    assert applied(con) == [p.name[:4] for p in MIGRATIONS]
    # Recorded, but nothing was created.
    n = con.execute(
        "select count(*) from information_schema.tables"
        " where table_schema='public' and table_name='stock_movement'"
    ).fetchone()[0]
    assert n == 0
    # And it is marked as not having been run by this tool.
    assert con.execute(
        "select bool_and(baselined) from public.schema_migrations"
    ).fetchone()[0] is True


def test_baseline_can_stop_at_a_version(blank):
    dsn, con = blank
    assert run(dsn, "baseline", "--through", "0015").returncode == 0
    assert applied(con) == [p.name[:4] for p in MIGRATIONS
                            if p.name[:4] <= "0015"]
    assert run(dsn, "verify").returncode == 1


def test_a_schema_ahead_of_the_checkout_is_named_but_allowed(blank):
    """A newer schema with older code usually works, and refusing to
    start would turn a rollback into an outage."""
    dsn, con = blank
    assert run(dsn, "apply").returncode == 0
    con.execute(
        "insert into public.schema_migrations (version, filename, checksum)"
        " values ('9990','0000_from_the_future.sql','deadbeefdeadbeef')")
    out = run(dsn, "verify")
    assert out.returncode == 0
    assert "ahead of this checkout" in out.stderr


def test_two_migrations_with_one_number_is_refused(blank):
    """Which runs first would depend on how the filenames sort."""
    dsn, _con = blank
    a = ROOT / "supabase" / "migrations" / "9996_one.sql"
    b = ROOT / "supabase" / "migrations" / "9996_two.sql"
    a.write_text("select 1;\n")
    b.write_text("select 1;\n")
    try:
        out = run(dsn, "status")
        assert out.returncode != 0
        assert "numbered 9996" in out.stdout + out.stderr
    finally:
        a.unlink()
        b.unlink()


def test_the_container_checks_the_schema_before_serving(blank):
    """The gate has to be in the start path, not a thing to remember."""
    start = (ROOT / "scripts" / "start.sh").read_text()
    assert "migrate.py verify" in start
    assert start.index("migrate.py verify") < start.index("uvicorn")
    # The word appears in the comment explaining the release order, so
    # look at what is actually run rather than the whole file.
    commands = [ln.strip() for ln in start.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    assert not any("migrate.py apply" in ln for ln in commands), (
        "the container must not apply migrations")
    docker = (ROOT / "Dockerfile").read_text()
    assert "scripts/start.sh" in docker
