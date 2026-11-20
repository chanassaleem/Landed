"""Tenant isolation and authentication.

These are the tests that matter most in the whole suite. Everything else
being wrong costs a wrong number on a screen. This being wrong shows one
distributor another distributor's receivables.

They run only when SOP_TEST_DSN points at a Postgres the suite may own.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

psycopg = pytest.importorskip("psycopg")

from engine.auth import (  # noqa: E402
    Principal, Role, WeakPassword, hash_password, hash_token,
    mask, new_session_token, verify_password,
)
from engine.db.pg_store import AuthError, PgStore  # noqa: E402
from engine.schema import ItemSnapshot  # noqa: E402

DSN = os.environ.get("SOP_TEST_DSN")           # the application role
ADMIN_DSN = os.environ.get("SOP_TEST_ADMIN_DSN")  # the schema owner
pytestmark = pytest.mark.skipif(
    not (DSN and ADMIN_DSN), reason="SOP_TEST_DSN / SOP_TEST_ADMIN_DSN not set"
)


@pytest.fixture(scope="module")
def store():
    s = PgStore(DSN, admin_dsn=ADMIN_DSN)
    s.install(app_password=os.environ.get("SOP_TEST_APP_PASSWORD"))
    with psycopg.connect(ADMIN_DSN) as con:      # clean slate between runs
        con.execute("TRUNCATE org CASCADE")
        con.commit()
    return s


@pytest.fixture(scope="module")
def two_orgs(store):
    a = store.create_org("Alpha Diagnostics")
    b = store.create_org("Beta Medical")
    store.create_user(a, "owner@alpha.test", "Alpha Owner", "alpha-pass-123", Role.OWNER)
    store.create_user(b, "owner@beta.test", "Beta Owner", "beta-pass-4567", Role.OWNER)
    pa = store.login("owner@alpha.test", "alpha-pass-123")[1]
    pb = store.login("owner@beta.test", "beta-pass-4567")[1]
    return store, pa, pb


# ------------------------------------------------------- the guarantee


def test_the_app_role_cannot_bypass_rls(store):
    """If this fails every other isolation test is meaningless."""
    with psycopg.connect(DSN) as con:
        row = con.execute(
            "SELECT current_user, "
            "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS super, "
            "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) AS bypass"
        ).fetchone()
    assert row[1] is False, "app role is a superuser; RLS would be ignored"
    assert row[2] is False, "app role has BYPASSRLS; RLS would be ignored"


def test_every_table_has_rls_enabled_and_data_tables_force_it(store):
    """Identity tables use ENABLE only, by design; data tables must FORCE."""
    with psycopg.connect(ADMIN_DSN) as con:
        rows = con.execute(
            """SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname='public' AND c.relkind='r'"""
        ).fetchall()
    data = {"entity", "location", "item", "snapshot", "fact_inventory",
            "fact_demand", "metric_run", "metric_result", "threshold_set",
            "audit"}
    identity = {"org", "app_user", "session"}
    seen = {r[0] for r in rows}
    assert (data | identity) <= seen

    for name, enabled, forced in rows:
        if name in (data | identity):
            assert enabled, f"{name} has no RLS at all"
        if name in data:
            assert forced, f"{name} does not FORCE RLS, so its owner bypasses it"


def test_every_tenant_table_carries_a_policy(store):
    with psycopg.connect(ADMIN_DSN) as con:
        policed = {r[0] for r in con.execute(
            "SELECT tablename FROM pg_policies WHERE schemaname='public'"
        ).fetchall()}
    expected = {"org", "entity", "location", "item", "snapshot",
                "fact_inventory", "fact_demand", "metric_run",
                "metric_result", "threshold_set", "audit", "app_user",
                "session"}
    assert expected <= policed, f"no RLS policy on {expected - policed}"


def test_the_application_role_does_not_own_any_table(store):
    """Ownership would exempt it from policies on tables without FORCE."""
    with psycopg.connect(ADMIN_DSN) as con:
        owned = con.execute(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname='public' AND tableowner = 'sop_app'"
        ).fetchall()
    assert owned == [], f"sop_app owns {[r[0] for r in owned]}"


def test_one_org_cannot_read_another(two_orgs):
    store, alpha, beta = two_orgs
    ea = store.ensure_entity(alpha, "Alpha Entity")
    la = store.ensure_location(alpha, ea, "Lahore")
    items = [ItemSnapshot("SECRET-1", "Alpha only", "reagent", 10, 0, 500, 30)]
    store.upsert_items(alpha, items)
    sid = store.add_snapshot(
        alpha, report_type="inventory", as_of=date(2026, 11, 1),
        entity_id=ea, location_id=la, row_count=1,
    )
    store.add_inventory_facts(alpha, sid, items)

    # Alpha sees its own row
    assert len(store.read_items(alpha, sid)) == 1
    # Beta, asking for the very same snapshot id, sees nothing
    assert store.read_items(beta, sid) == []
    assert store.latest_snapshot_id(beta, "inventory") is None
    assert all(s["snapshot_id"] != sid for s in store.snapshots(beta))


def test_a_raw_query_in_the_wrong_context_returns_nothing(two_orgs):
    """Isolation must hold even if application code forgets to filter."""
    store, alpha, beta = two_orgs
    with store.tenant(beta.org_id) as con:
        rows = con.execute("SELECT * FROM item").fetchall()   # no WHERE at all
    assert all(str(r["org_id"]) == beta.org_id for r in rows)
    assert not any(r["item_key"] == "SECRET-1" for r in rows)


def test_missing_org_context_sees_nothing_rather_than_everything(store):
    """The failure mode for an unset context must be closed, not open."""
    with psycopg.connect(DSN) as con:
        with con.transaction():
            rows = con.execute("SELECT * FROM item").fetchall()
    assert rows == []


def test_context_does_not_leak_between_transactions(two_orgs):
    """SET LOCAL must not survive onto the next use of a pooled connection."""
    store, alpha, beta = two_orgs
    with store.tenant(alpha.org_id) as con:
        assert con.execute("SELECT app_current_org() AS o").fetchone()["o"]
    with psycopg.connect(DSN) as con:
        with con.transaction():
            assert con.execute("SELECT app_current_org()").fetchone()[0] is None


def test_writing_under_the_wrong_org_is_refused(two_orgs):
    """WITH CHECK must stop a row being written into someone else's org."""
    store, alpha, beta = two_orgs
    with pytest.raises(psycopg.errors.Error):
        with store.tenant(beta.org_id) as con:
            con.execute(
                "INSERT INTO item (org_id, item_key, description) VALUES (%s,%s,%s)",
                (alpha.org_id, "SMUGGLED", "written into the wrong org"),
            )


# ------------------------------------------------------------- auth


def test_password_is_argon2id_and_verifies():
    h = hash_password("a-good-long-password")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "a-good-long-password")
    assert not verify_password(h, "a-good-long-passwore")


def test_weak_passwords_are_refused():
    with pytest.raises(WeakPassword):
        hash_password("short")


def test_the_raw_session_token_is_never_stored(two_orgs):
    store, alpha, _ = two_orgs
    token, _ = store.login("owner@alpha.test", "alpha-pass-123")
    # Inspected through the owner connection on purpose: the application
    # role has no org context here, so RLS would correctly return nothing.
    with psycopg.connect(ADMIN_DSN) as con:
        rows = con.execute("SELECT token_hash FROM session").fetchall()
    stored = {r[0] for r in rows}
    assert token not in stored
    assert hash_token(token) in stored


def test_session_resolves_then_stops_after_logout(two_orgs):
    store, _, _ = two_orgs
    token, p = store.login("owner@alpha.test", "alpha-pass-123")
    assert store.resolve(token).user_id == p.user_id
    store.logout(token)
    assert store.resolve(token) is None


def test_bad_credentials_are_indistinguishable(two_orgs):
    store, _, _ = two_orgs
    msgs = set()
    for email, pw in [("owner@alpha.test", "wrong-password-x"),
                      ("nobody@nowhere.test", "whatever-123")]:
        with pytest.raises(AuthError) as e:
            store.login(email, pw)
        msgs.add(str(e.value))
    assert len(msgs) == 1, "error text reveals whether the account exists"


def test_a_garbage_token_resolves_to_nobody(store):
    assert store.resolve(new_session_token()) is None
    assert store.resolve("") is None


# ------------------------------------------------- role field masking


def _principal(role: Role) -> Principal:
    return Principal("u", "o", "a@b.test", "Tester", role, "s")


def test_service_role_never_receives_money_fields():
    payload = {"kpis": {"stock_value": 21_000_000, "critical": 1},
               "rows": [{"item_key": "X", "on_hand": 5, "unit_cost": 500,
                         "order_value": 2500}]}
    out = mask(payload, _principal(Role.SERVICE))
    assert "stock_value" not in out["kpis"]
    assert out["kpis"]["critical"] == 1          # non-sensitive survives
    assert "unit_cost" not in out["rows"][0]
    assert "order_value" not in out["rows"][0]
    assert out["rows"][0]["on_hand"] == 5


def test_owner_receives_everything():
    payload = {"kpis": {"stock_value": 1, "cash_trapped": 2}}
    assert mask(payload, _principal(Role.OWNER)) == payload


def test_masking_removes_rather_than_nulls():
    """A null would let the client tell a withheld figure from a real zero."""
    out = mask({"unit_cost": 0}, _principal(Role.SERVICE))
    assert "unit_cost" not in out


def test_roles_see_different_module_sets():
    owner = _principal(Role.OWNER).modules()
    service = _principal(Role.SERVICE).modules()
    viewer = _principal(Role.VIEWER).modules()
    assert "receivables" in owner and "receivables" not in service
    assert "upload" in owner and "upload" not in viewer
    assert service < owner and viewer < owner


def test_the_two_schemas_agree_on_discovery(store):
    """The DuckDB and Postgres schemas are maintained side by side, and a
    column added to one and forgotten in the other is how they drift."""
    from pathlib import Path
    duck = (Path(__file__).resolve().parent.parent
            / "engine" / "db" / "schema.sql").read_text()
    assert "configured" in duck
    with psycopg.connect(ADMIN_DSN) as con:
        for table in ("entity", "location"):
            cols = {r[0] for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s", (table,)).fetchall()}
            assert "configured" in cols, f"{table} has no configured column"
