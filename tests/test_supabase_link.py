"""The Supabase connection, and the health probe that guards it.

The probe is the interesting part. A database with row-level security
switched off answers `select 1` happily, so a connectivity check reports
green on a project that is handing every tenant's rows to anyone who
asks. These tests prove the probe tells those two states apart -- by
actually turning RLS off and watching it go red.

They need a Postgres, and skip without one.
"""

from __future__ import annotations

import os
import pathlib
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from engine.db import supabase as sb  # noqa: E402

DSN = os.environ.get("LANDED_TEST_DSN")
ROOT = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- redaction
#
# These need no database: a password must never reach a log line, and
# that has to hold before anything is connected to.


def test_a_password_never_survives_redaction():
    dsn = "postgresql://postgres.abcdef:sup3r-s3cret@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres"
    out = sb.redact(dsn)
    assert "sup3r-s3cret" not in out
    assert "aws-0-ap-southeast-2.pooler.supabase.com" in out, (
        "redaction removed more than the password, which makes errors useless"
    )


def test_the_config_repr_cannot_leak_the_password():
    """An exception handler printing a repr is the usual way this escapes."""
    cfg = sb.Config(dsn="postgresql://postgres:hunter2@db.example.co:5432/postgres")
    assert "hunter2" not in repr(cfg)
    assert "hunter2" not in str(cfg)


def test_the_service_role_key_is_not_held_on_the_config():
    """It is read where it is used, so there is nothing to leak."""
    cfg = sb.Config.from_env({
        "DATABASE_URL": "postgresql://u:p@h:5432/d",
        "SUPABASE_SERVICE_ROLE_KEY": "service-role-secret",
    })
    assert "service-role-secret" not in repr(cfg)
    assert not any(
        "service-role-secret" in str(v) for v in vars(cfg).values()
    )


# ------------------------------------------------------- configuration help


def test_the_direct_connection_string_is_flagged():
    """IPv6-only without the paid add-on, and Render is IPv4-only.

    The failure it causes is a name-resolution error that looks nothing
    like its cause, so it is worth catching in configuration instead.
    """
    cfg = sb.Config(
        dsn="postgresql://postgres:p@db.hyqutwoymrepdxqouyru.supabase.co:5432/postgres"
    )
    assert any("IPv6-only" in w for w in cfg.warnings)


def test_the_session_pooler_string_is_not_flagged_for_ipv6(monkeypatch):
    cfg = sb.Config(
        dsn="postgresql://postgres.ref:p@aws-0-ap-southeast-2.pooler.supabase.com"
            ":5432/postgres?sslmode=require"
    )
    assert not any("IPv6" in w for w in cfg.warnings)


def test_the_transaction_pooler_is_flagged():
    """Port 6543 keeps no session state.

    as_user sets the role and the claims with SET LOCAL inside a
    transaction. That survives transaction mode, but the mode is still
    the wrong tool for a long-lived backend, and the next person to add
    a session-scoped setting would find out the hard way.
    """
    cfg = sb.Config(
        dsn="postgresql://postgres.ref:p@aws-0-ap-southeast-2.pooler.supabase.com"
            ":6543/postgres?sslmode=require"
    )
    assert any("6543" in w for w in cfg.warnings)


def test_no_database_url_is_reported_rather_than_guessed(monkeypatch):
    assert sb.Config.from_env({}) is None


# ----------------------------------------------------------- the real thing

pg = pytest.mark.skipif(not DSN, reason="LANDED_TEST_DSN not set")


@pytest.fixture
def fresh(request):
    """A database with the schema applied, and a Database over it."""
    con = psycopg.connect(DSN, autocommit=True)
    con.execute("drop schema if exists public cascade")
    con.execute("drop schema if exists app cascade")
    con.execute("drop schema if exists auth cascade")
    con.execute("create schema public")
    for path in [
        ROOT / "supabase" / "tests" / "00_supabase_shim.sql",
        *sorted((ROOT / "supabase" / "migrations").glob("*.sql")),
    ]:
        con.execute(path.read_text())
    db = sb.Database(sb.Config(dsn=DSN))
    yield db, con
    db.close()
    con.close()


@pg
def test_the_probe_is_green_against_a_healthy_project(fresh):
    db, _ = fresh
    health = db.check()
    assert health.reachable
    assert health.rls_holding is True
    assert health.ok
    assert health.latency_ms is not None and health.latency_ms >= 0


@pg
def test_the_probe_goes_red_when_row_level_security_is_switched_off(fresh):
    """The test that gives the probe its value.

    Everything else about the database is unchanged and perfectly
    healthy -- it connects, it answers, it has data. Only the protection
    is gone, and the probe is the one check that notices.
    """
    db, con = fresh
    assert db.check().ok

    con.execute("alter table public.items disable row level security")
    # Something for an unauthorised caller to find. created_by is NOT
    # NULL since 0005: an organisation with no creator has no owner.
    somebody = uuid.uuid4()
    con.execute(
        "insert into auth.users (id, email) values (%s, 'exposed@test')", (somebody,)
    )
    org = con.execute(
        "insert into public.organisations (name, created_by)"
        " values ('Exposed', %s) returning id",
        (somebody,),
    ).fetchone()[0]
    con.execute(
        "insert into public.items (org_id, sku, description)"
        " values (%s, 'LEAK-1', 'Visible to anyone')",
        (org,),
    )

    health = db.check()
    assert health.reachable, "the database is still perfectly reachable"
    assert health.rls_holding is False
    assert not health.ok
    assert "not protecting" in health.detail

    con.execute("alter table public.items enable row level security")
    assert db.check().ok


@pg
def test_an_unreachable_database_reports_instead_of_raising(fresh):
    """A health endpoint that raises tells you nothing from the outside."""
    db = sb.Database(
        sb.Config(dsn="postgresql://nobody:nothing@127.0.0.1:1/absent"),
    )
    health = db.check()
    assert not health.reachable
    assert health.rls_holding is None
    assert not health.ok
    assert health.detail


@pg
def test_an_error_message_never_carries_the_password(fresh):
    db = sb.Database(
        sb.Config(dsn="postgresql://nobody:hunter2@127.0.0.1:1/absent"),
    )
    assert "hunter2" not in db.check().detail


@pg
def test_as_user_scopes_the_session_and_does_not_leak_it(fresh):
    """SET LOCAL, so the next borrower of the pooled connection is clean.

    On a pool the next borrower is another customer's request, which is
    what makes this a tenancy property rather than a tidiness one.
    """
    db, con = fresh
    someone = uuid.uuid4()
    con.execute("insert into auth.users (id, email) values (%s, 'a@b.c')", (someone,))

    with db.as_user(someone) as c:
        assert c.execute("select auth.uid()").fetchone()[0] == someone
        assert c.execute("select current_user").fetchone()[0] == "authenticated"

    # Same pool, fresh transaction: the claims must be gone.
    with db.as_service() as c:
        assert c.execute("select auth.uid()").fetchone()[0] is None
        assert c.execute("select current_user").fetchone()[0] != "authenticated"


@pg
def test_as_user_is_subject_to_the_policies(fresh):
    """The whole point of going through this module rather than psycopg."""
    db, con = fresh
    owner = uuid.uuid4()
    stranger = uuid.uuid4()
    for uid in (owner, stranger):
        con.execute(
            "insert into auth.users (id, email) values (%s, %s)",
            (uid, f"{uid.hex[:6]}@test"),
        )
    with db.as_user(owner) as c:
        org = c.execute(
            "insert into public.organisations (name, created_by)"
            " values ('Mine', %s) returning id",
            (owner,),
        ).fetchone()[0]
        c.execute(
            "insert into public.items (org_id, sku, description)"
            " values (%s, 'MINE-1', 'Mine') ",
            (org,),
        )

    with db.as_user(stranger) as c:
        assert c.execute("select count(*) from public.items").fetchone()[0] == 0


@pg
def test_as_service_bypasses_the_policies_as_its_name_warns(fresh):
    db, con = fresh
    owner = uuid.uuid4()
    con.execute("insert into auth.users (id, email) values (%s, 'o@t.c')", (owner,))
    with db.as_user(owner) as c:
        org = c.execute(
            "insert into public.organisations (name, created_by)"
            " values ('Mine', %s) returning id", (owner,),
        ).fetchone()[0]
        c.execute(
            "insert into public.items (org_id, sku, description)"
            " values (%s, 'MINE-1', 'Mine')", (org,),
        )
    with db.as_service() as c:
        assert c.execute("select count(*) from public.items").fetchone()[0] == 1


# ------------------------------------------------------------- the endpoint


def test_the_endpoint_reports_503_when_nothing_is_configured(monkeypatch):
    """Unconfigured is a failure, not a pass.

    Returning 200 with "not configured" is how a service sits broken in
    production with a green tick beside it.
    """
    from fastapi.testclient import TestClient

    import web.app as webapp

    sb.reset()
    for key in ("DATABASE_URL", "LANDED_TEST_DSN"):
        monkeypatch.delenv(key, raising=False)

    with TestClient(webapp.app, raise_server_exceptions=False) as client:
        r = client.get("/health/supabase")
    sb.reset()

    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False and body["configured"] is False
    assert "DATABASE_URL" in body["detail"]


@pg
def test_the_endpoint_is_green_against_a_healthy_project(monkeypatch, fresh):
    from fastapi.testclient import TestClient

    import web.app as webapp

    sb.reset()
    monkeypatch.setenv("DATABASE_URL", DSN)
    with TestClient(webapp.app, raise_server_exceptions=False) as client:
        r = client.get("/health/supabase")
    sb.reset()

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["rls_holding"] is True
    assert body["configured"] is True


def test_the_plain_health_endpoint_still_needs_no_database():
    """It is what tells you the app is up when the database is not."""
    from fastapi.testclient import TestClient

    import web.app as webapp

    with TestClient(webapp.app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
