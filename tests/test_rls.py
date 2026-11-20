"""The policies, tested against a real Postgres.

These are the tests that matter most in the repository. Everything else
being wrong costs a wrong number on a screen. These being wrong shows one
importer another importer's stock, costs and suppliers.

Reading the SQL is not evidence that it works. An RLS policy can be
present, syntactically valid, and still let everything through -- a
missing USING clause, a view that runs as its owner, a SECURITY DEFINER
helper that was meant to be scoped and is not. The only way to know is to
connect as a real user with real claims and try to read someone else's
rows.

They run wherever a Postgres is reachable, and skip when one is not:

    LANDED_TEST_DSN='postgres://postgres@/landed?host=/tmp&port=5441' \\
        python3 -m pytest tests/test_rls.py
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("LANDED_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="LANDED_TEST_DSN not set")

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
SHIM = ROOT / "supabase" / "tests" / "00_supabase_shim.sql"


# --------------------------------------------------------------- harness


@pytest.fixture(scope="module")
def db():
    """A database with the schema applied, rebuilt from the migrations.

    Rebuilt rather than cleaned, because a test that proves isolation has
    to run against the schema as a migration actually creates it, not
    against whatever the last run left behind.
    """
    con = psycopg.connect(DSN, autocommit=True)
    con.execute("drop schema if exists public cascade")
    con.execute("drop schema if exists app cascade")
    con.execute("drop schema if exists auth cascade")
    con.execute("create schema public")
    for path in [SHIM, *MIGRATIONS]:
        con.execute(path.read_text())
    yield con
    con.close()


@contextlib.contextmanager
def as_user(con, user_id):
    """Act as that signed-in user for the length of the block.

    SET LOCAL inside a transaction, so neither the role nor the claims
    leak into the next test the way a session-level SET would. A context
    manager rather than a pair of calls because one refused statement
    poisons the connection, and a test that forgets to unwind takes every
    test after it down with it.
    """
    with con.transaction():
        con.execute("set local role authenticated")
        con.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (json.dumps({"sub": str(user_id), "role": "authenticated"}),),
        )
        yield con


def new_user(con, email):
    uid = uuid.uuid4()
    con.execute("insert into auth.users (id, email) values (%s, %s)", (uid, email))
    return uid


def new_org(con, owner, name):
    """An organisation and its first branch, created the way the app does."""
    with as_user(con, owner):
        org = con.execute(
            "insert into public.organisations (name, created_by)"
            " values (%s, %s) returning id",
            (name, owner),
        ).fetchone()[0]
        loc = con.execute(
            "insert into public.locations (org_id, code, name)"
            " values (%s, 'MAIN', 'Main') returning id",
            (org,),
        ).fetchone()[0]
    return org, loc


def sku(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def world(db):
    """Two importers who must never see each other, and their people."""
    anwar = new_user(db, "anwar@importer-a.test")
    bilal = new_user(db, "bilal@importer-b.test")
    clerk = new_user(db, "clerk@importer-a.test")
    outsider = new_user(db, "nobody@elsewhere.test")

    org_a, loc_a = new_org(db, anwar, "Importer A")
    org_b, loc_b = new_org(db, bilal, "Importer B")

    with as_user(db, anwar):
        loc_a2 = db.execute(
            "insert into public.locations (org_id, code, name)"
            " values (%s, 'KHI', 'Karachi') returning id",
            (org_a,),
        ).fetchone()[0]
        # A clerk who may work at the main branch only.
        db.execute(
            "insert into public.memberships (org_id, user_id, role, location_ids)"
            " values (%s, %s, 'staff', %s)",
            (org_a, clerk, [loc_a]),
        )
        item_a = db.execute(
            "insert into public.items (org_id, sku, description)"
            " values (%s, 'A-1', 'Reagent A') returning id",
            (org_a,),
        ).fetchone()[0]

    with as_user(db, bilal):
        item_b = db.execute(
            "insert into public.items (org_id, sku, description)"
            " values (%s, 'B-1', 'Reagent B') returning id",
            (org_b,),
        ).fetchone()[0]

    return dict(
        anwar=anwar, bilal=bilal, clerk=clerk, outsider=outsider,
        org_a=org_a, org_b=org_b, loc_a=loc_a, loc_a2=loc_a2, loc_b=loc_b,
        item_a=item_a, item_b=item_b,
    )


# --------------------------------------------------------- creating an org


def test_a_new_user_can_create_an_organisation_and_read_it_back(db):
    """INSERT ... RETURNING, which is what supabase-js does by default.

    RETURNING has to pass the SELECT policy on the new row, and the
    membership that would grant it is created by an AFTER trigger the
    statement's own snapshot cannot see. This is the first call every new
    customer makes, and it failed until the policy accounted for it.
    """
    founder = new_user(db, f"founder-{uuid.uuid4().hex[:6]}@test")
    with as_user(db, founder):
        row = db.execute(
            "insert into public.organisations (name, created_by)"
            " values ('Signed Up Just Now', %s) returning id, name",
            (founder,),
        ).fetchone()
    assert row is not None and row[1] == "Signed Up Just Now"


def test_a_new_organisation_gets_its_owner_and_the_importer_modules(db):
    founder = new_user(db, f"founder-{uuid.uuid4().hex[:6]}@test")
    org, _ = new_org(db, founder, "Fresh Importer")

    role = db.execute(
        "select role from public.memberships where org_id = %s and user_id = %s",
        (org, founder),
    ).fetchone()
    assert role and role[0] == "owner", (
        "an organisation was created with no owner, so nobody can administer it"
    )

    modules = {
        r[0] for r in db.execute(
            "select module from public.entitlements where org_id = %s", (org,)
        ).fetchall()
    }
    assert modules == {"stock", "buy", "freight", "demand", "cash"}


# --------------------------------------------------------- the tenant wall


def test_an_owner_sees_only_their_own_organisation(db, world):
    with as_user(db, world["anwar"]):
        names = [r[0] for r in db.execute(
            "select name from public.organisations where id in (%s, %s)",
            (world["org_a"], world["org_b"]),
        ).fetchall()]
    assert names == ["Importer A"]


def test_an_importer_cannot_see_another_importers_items(db, world):
    with as_user(db, world["bilal"]):
        skus = [r[0] for r in db.execute("select sku from public.items").fetchall()]
    assert skus == ["B-1"]


def test_naming_the_other_tenants_row_directly_still_returns_nothing(db, world):
    """Guessing an id is the obvious attack.

    A policy that only hides rows from the default listing leaves it wide
    open, and the listing is the only thing most people test.
    """
    with as_user(db, world["bilal"]):
        rows = db.execute(
            "select sku from public.items where id = %s", (world["item_a"],)
        ).fetchall()
    assert rows == []


def test_a_signed_in_stranger_sees_nothing_at_all(db, world):
    with as_user(db, world["outsider"]):
        counts = db.execute(
            "select (select count(*) from public.organisations),"
            "       (select count(*) from public.items),"
            "       (select count(*) from public.stock_movement)"
        ).fetchone()
    assert counts == (0, 0, 0)


def test_writing_a_row_into_someone_elses_organisation_is_refused(db, world):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_user(db, world["bilal"]):
            db.execute(
                "insert into public.items (org_id, sku, description)"
                " values (%s, 'STOLEN', 'Should not land')",
                (world["org_a"],),
            )


# ----------------------------------------------------------------- views
#
# A view without security_invoker runs with its OWNER's rights and serves
# every tenant's rows to any caller. It is the quietest way to undo every
# policy above, so each one gets its own check.


@pytest.mark.parametrize(
    "view",
    ["stock_on_hand", "item_cost", "demand_monthly",
     "shipment_line_basis", "shipment_totals", "landed_cost"],
)
def test_every_view_runs_as_the_caller_not_as_its_owner(db, view):
    row = db.execute(
        "select c.reloptions from pg_class c"
        "  join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname = 'public' and c.relkind = 'v' and c.relname = %s",
        (view,),
    ).fetchone()
    assert row is not None, f"{view} does not exist"
    assert row[0] and "security_invoker=true" in row[0], (
        f"{view} does not set security_invoker, so it will serve every "
        f"tenant's rows to any caller"
    )


def test_the_on_hand_view_is_filtered_per_tenant(db, world):
    with as_user(db, world["anwar"]):
        db.execute(
            "insert into public.stock_movement"
            " (org_id, location_id, item_id, qty, unit_cost, reason)"
            " values (%s, %s, %s, 10, 100, 'opening')",
            (world["org_a"], world["loc_a"], world["item_a"]),
        )
    with as_user(db, world["bilal"]):
        rows = db.execute(
            "select * from public.stock_on_hand where item_id = %s",
            (world["item_a"],),
        ).fetchall()
    assert rows == []


# ------------------------------------------------------- the ledger rules


def test_an_owners_attempt_to_edit_the_ledger_changes_nothing(db, world):
    """Note what this does NOT assert: an error.

    stock_movement has no UPDATE policy, and a row no policy admits is
    simply filtered out of the statement -- so the update touches zero
    rows and Postgres raises nothing. The data is safe, but a client that
    only checks for an exception will believe it succeeded, which is why
    the API must treat a zero rowcount here as a failure and why this
    test pins the rowcount rather than the error.
    """
    with as_user(db, world["anwar"]):
        mv = db.execute(
            "insert into public.stock_movement"
            " (org_id, location_id, item_id, qty, reason)"
            " values (%s, %s, %s, 5, 'opening') returning id",
            (world["org_a"], world["loc_a"], world["item_a"]),
        ).fetchone()[0]

    with as_user(db, world["anwar"]):
        cur = db.execute(
            "update public.stock_movement set qty = 500 where id = %s", (mv,)
        )
        assert cur.rowcount == 0, "an owner was able to rewrite history"

    after = db.execute(
        "select qty from public.stock_movement where id = %s", (mv,)
    ).fetchone()[0]
    assert float(after) == 5


def test_the_ledger_cannot_be_deleted_by_the_service_role_either(db, world):
    """The trigger, not the policy, is what makes this true.

    A policy stops a signed-in user. The service role bypasses every
    policy, which is exactly why history has to be defended a layer
    further down than RLS.
    """
    mv = db.execute(
        "insert into public.stock_movement"
        " (org_id, location_id, item_id, qty, reason)"
        " values (%s, %s, %s, 7, 'opening') returning id",
        (world["org_a"], world["loc_a"], world["item_a"]),
    ).fetchone()[0]
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("delete from public.stock_movement where id = %s", (mv,))


def test_a_replayed_offline_write_does_not_double_count(db, world):
    """The same queued movement arriving twice after a flaky reconnect.

    This is the property that makes an append-only ledger safe to sync
    from a phone: the second copy is refused, not added.
    """
    ref = f"phone-{uuid.uuid4().hex[:10]}"
    args = (world["org_a"], world["loc_a"], world["item_a"], ref)
    sql = ("insert into public.stock_movement"
           " (org_id, location_id, item_id, qty, reason, client_ref)"
           " values (%s, %s, %s, -3, 'sale', %s)")

    with as_user(db, world["anwar"]):
        db.execute(sql, args)
    with pytest.raises(psycopg.errors.UniqueViolation):
        with as_user(db, world["anwar"]):
            db.execute(sql, args)

    count = db.execute(
        "select count(*) from public.stock_movement where client_ref = %s", (ref,)
    ).fetchone()[0]
    assert count == 1


# --------------------------------------------------------- branch scoping


def test_a_branch_clerk_sees_only_their_own_branch(db, world):
    """The case a tenant-only wall gets wrong.

    The clerk is a real member of Importer A, so org_id matches on every
    row. Without the location check they would see every branch's stock.
    """
    with as_user(db, world["clerk"]):
        codes = [r[0] for r in db.execute(
            "select code from public.locations order by code"
        ).fetchall()]
    assert codes == ["MAIN"]


def test_a_clerk_cannot_record_stock_at_a_branch_they_are_not_at(db, world):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_user(db, world["clerk"]):
            db.execute(
                "insert into public.stock_movement"
                " (org_id, location_id, item_id, qty, reason)"
                " values (%s, %s, %s, -1, 'sale')",
                (world["org_a"], world["loc_a2"], world["item_a"]),
            )


def test_a_clerk_can_record_stock_at_their_own_branch(db, world):
    """A wall that lets nobody through is not access control either."""
    with as_user(db, world["clerk"]):
        db.execute(
            "insert into public.stock_movement"
            " (org_id, location_id, item_id, qty, reason)"
            " values (%s, %s, %s, -1, 'sale')",
            (world["org_a"], world["loc_a"], world["item_a"]),
        )


def test_a_clerk_cannot_change_policy(db, world):
    """Staff record what happened. They do not decide what the rules are."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_user(db, world["clerk"]):
            db.execute(
                "insert into public.items (org_id, sku, description)"
                " values (%s, 'CLERK-1', 'Should not land')",
                (world["org_a"],),
            )


# ------------------------------------------------------------ entitlements


def test_a_module_that_is_not_paid_for_cannot_be_reached(db, world):
    """Hiding a menu item is not access control.

    If the only thing stopping someone is navigation, the module is
    reachable by typing a URL. The entitlement has to sit in the same
    policy as the tenant wall.
    """
    with as_user(db, world["anwar"]):
        before = db.execute("select count(*) from public.items").fetchone()[0]
    assert before > 0

    db.execute(
        "update public.entitlements set status = 'lapsed'"
        " where org_id = %s and module = 'stock'",
        (world["org_a"],),
    )
    try:
        with as_user(db, world["anwar"]):
            after = db.execute("select count(*) from public.items").fetchone()[0]
        assert after == 0, "a lapsed module still served its rows"
    finally:
        db.execute(
            "update public.entitlements set status = 'active'"
            " where org_id = %s and module = 'stock'",
            (world["org_a"],),
        )


def test_an_entitlement_lapses_on_its_own_date(db, world):
    db.execute(
        "update public.entitlements set valid_until = current_date - 1"
        " where org_id = %s and module = 'freight'",
        (world["org_a"],),
    )
    try:
        with as_user(db, world["anwar"]):
            seen = db.execute("select count(*) from public.shipments").fetchone()[0]
        assert seen == 0
    finally:
        db.execute(
            "update public.entitlements set valid_until = null"
            " where org_id = %s and module = 'freight'",
            (world["org_a"],),
        )


# -------------------------------------------------------- receiving goods


def test_receiving_goods_posts_the_stock_movement_itself(db, world):
    """The GRN is the document; the movement is the consequence.

    Posted in the database rather than in the API, so that an import, a
    phone and a script cannot each forget it in their own way.
    """
    org, loc = world["org_a"], world["loc_a"]
    with as_user(db, world["anwar"]):
        item = db.execute(
            "insert into public.items (org_id, sku, description)"
            " values (%s, %s, 'Received') returning id", (org, sku("RCV"))
        ).fetchone()[0]
        grn = db.execute(
            "insert into public.goods_receipts (org_id, location_id, grn_number)"
            " values (%s, %s, %s) returning id", (org, loc, sku("GRN"))
        ).fetchone()[0]
        line = db.execute(
            "insert into public.grn_lines (org_id, grn_id, item_id, qty, unit_price)"
            " values (%s, %s, %s, 25, 400) returning id", (org, grn, item)
        ).fetchone()[0]

        mv = db.execute(
            "select qty, unit_cost, reason, source_type"
            "  from public.stock_movement where source_id = %s", (line,)
        ).fetchone()
        on_hand = db.execute(
            "select on_hand from public.stock_on_hand where item_id = %s", (item,)
        ).fetchone()

    assert mv is not None, "receiving goods did not move any stock"
    assert float(mv[0]) == 25 and float(mv[1]) == 400
    assert (mv[2], mv[3]) == ("grn", "grn_line")
    assert float(on_hand[0]) == 25


# ------------------------------------------------------------ landed cost


def test_landed_cost_spreads_each_charge_on_its_own_basis(db, world):
    """Duty follows value; ocean freight follows volume.

    Two lines of equal value and very different volume. One basis for
    everything -- the usual shortcut -- would charge them the same
    freight, which is how a bulky cheap line ends up subsidised by a
    compact expensive one.
    """
    org, loc = world["org_a"], world["loc_a"]
    with as_user(db, world["anwar"]):
        items = {
            name: db.execute(
                "insert into public.items (org_id, sku, description)"
                " values (%s, %s, %s) returning id", (org, sku(name.upper()), name)
            ).fetchone()[0]
            for name in ("bulky", "compact")
        }
        grn = db.execute(
            "insert into public.goods_receipts (org_id, location_id, grn_number)"
            " values (%s, %s, %s) returning id", (org, loc, sku("GRN"))
        ).fetchone()[0]
        lines = {
            name: db.execute(
                "insert into public.grn_lines"
                " (org_id, grn_id, item_id, qty, unit_price)"
                " values (%s, %s, %s, 10, 1000) returning id", (org, grn, item)
            ).fetchone()[0]
            for name, item in items.items()
        }
        ship = db.execute(
            "insert into public.shipments (org_id, reference, mode)"
            " values (%s, %s, 'sea') returning id", (org, sku("SH"))
        ).fetchone()[0]
        for name, volume in (("bulky", 9), ("compact", 1)):
            db.execute(
                "insert into public.shipment_lines"
                " (shipment_id, grn_line_id, org_id, weight_kg, volume_cbm)"
                " values (%s, %s, %s, 100, %s)", (ship, lines[name], org, volume)
            )
        # Equal value, so duty splits evenly. Volume is 9:1, so freight does not.
        db.execute(
            "insert into public.shipment_costs"
            " (org_id, shipment_id, kind, amount, allocate_by)"
            " values (%s, %s, 'duty', 2000, 'value')", (org, ship))
        db.execute(
            "insert into public.shipment_costs"
            " (org_id, shipment_id, kind, amount, allocate_by)"
            " values (%s, %s, 'freight', 1000, 'volume')", (org, ship))

        got = {
            r[0]: (float(r[1]), float(r[2]))
            for r in db.execute(
                "select grn_line_id, allocated_cost, landed_unit_cost"
                "  from public.landed_cost where grn_line_id = any(%s)",
                ([lines["bulky"], lines["compact"]],),
            ).fetchall()
        }

    # Duty 1000 each; freight 900 and 100 on the 9:1 volume split.
    assert got[lines["bulky"]][0] == pytest.approx(1900)
    assert got[lines["compact"]][0] == pytest.approx(1100)
    # Goods were 10 x 1000 on each line, so 10,000 before freight.
    assert got[lines["bulky"]][1] == pytest.approx(1190)
    assert got[lines["compact"]][1] == pytest.approx(1110)


def test_landed_cost_is_not_visible_to_another_importer(db, world):
    with as_user(db, world["bilal"]):
        rows = db.execute("select count(*) from public.landed_cost").fetchone()[0]
    assert rows == 0
