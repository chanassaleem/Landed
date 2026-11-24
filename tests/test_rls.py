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
from datetime import date, timedelta

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
    # Migration 0009 widened the wedge from five modules to all
    # thirteen. Two of the eight it adds -- equipment and service --
    # already had screens in the app and no entitlement to license
    # them, so checking entitlements against a five-module trial would
    # have locked two working screens on every account. The honest
    # reason for granting the rest is that billing does not exist:
    # withholding modules nobody is charged for would be a paywall
    # with no payment behind it.
    from engine.platform import MODULES

    # Compared to the registry rather than to a count. A literal here
    # has to be edited every time a module is added, which makes the
    # test a chore rather than a guard -- and a chore gets updated
    # without being thought about.
    assert modules == {m.id for m in MODULES}
    assert len(modules) == len(MODULES)


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


# ------------------------------------------- the tables added since 0008
#
# Ten tables arrived with migrations 0009 to 0012 -- the statements,
# commitments, bank balances, payroll, the pipeline, contacts and
# dealings -- and none of them was in this file. Each carries a policy
# and every policy was written carefully, which is exactly the state a
# missing one hides in.
#
# Two tests. The structural one catches the table somebody adds next
# year and forgets; the behavioural one proves the policies on these
# ten actually refuse, because a policy can be present, syntactically
# valid, and let everything through.


def test_every_tenant_table_has_row_level_security_and_a_read_policy(db):
    """A table with an org_id and no policy is a table that leaks.

    Structural rather than per-table on purpose: a test naming the
    tables has to be remembered, and the one that matters is the one
    added after everybody stopped thinking about this.
    """
    rows = db.execute(
        "select c.relname, c.relrowsecurity,"
        "       (select count(*) from pg_policy p"
        "         where p.polrelid = c.oid"
        "           and p.polcmd in ('r', '*')) as read_policies"
        "  from pg_class c"
        "  join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname = 'public' and c.relkind = 'r'"
        "   and exists (select 1 from pg_attribute a"
        "                where a.attrelid = c.oid"
        "                  and a.attname = 'org_id'"
        "                  and not a.attisdropped)"
        " order by c.relname"
    ).fetchall()
    assert rows, "no tenant tables found, so this test proves nothing"

    unguarded = [r[0] for r in rows if not r[1]]
    assert not unguarded, f"row-level security is off on: {unguarded}"

    unread = [r[0] for r in rows if r[2] == 0]
    assert not unread, f"no read policy on: {unread}"


def test_one_importer_cannot_read_the_others_money_or_people(db):
    """The behavioural half, on the tables added since 0008.

    A trial balance, a payroll and a pipeline are the three things a
    competitor would most like to see, and all three arrived without a
    test in this file.
    """
    anwar = new_user(db, f"anwar-{uuid.uuid4().hex[:6]}@a.test")
    bilal = new_user(db, f"bilal-{uuid.uuid4().hex[:6]}@b.test")
    org_a, loc_a = new_org(db, anwar, "Importer A")
    org_b, _loc_b = new_org(db, bilal, "Importer B")

    with as_user(db, anwar) as c:
        cust = c.execute(
            "insert into public.customers (org_id, code, name)"
            " values (%s, 'SECRET', 'A big hospital') returning id",
            (org_a,)).fetchone()[0]
        c.execute(
            "insert into public.accounts (org_id, code, name, line,"
            " period_from, period_to, opening, closing)"
            " values (%s,'4000','Sales','revenue','2026-01-01',"
            "'2026-12-31',0,48000000)", (org_a,))
        c.execute(
            "insert into public.equity_movements (org_id, kind, moved_on,"
            " amount) values (%s,'dividend','2026-06-30',1000000)",
            (org_a,))
        c.execute(
            "insert into public.commitments (org_id, label, kind,"
            " due_date, amount) values (%s,'LC Roche','lc_settlement',"
            "'2026-12-01',5250000)", (org_a,))
        c.execute(
            "insert into public.cash_balances (org_id, as_at, balance)"
            " values (%s,'2026-11-30',3250000)", (org_a,))
        c.execute(
            "insert into public.employees (org_id, employee_no, name,"
            " basic_monthly) values (%s,'E-1','Imran Shah',185000)",
            (org_a,))
        c.execute(
            "insert into public.opportunities (org_id, customer_id,"
            " reference, stage, value)"
            " values (%s,%s,'TND-1','tender_submitted',9800000)",
            (org_a, cust))
        c.execute(
            "insert into public.marketing_spend (org_id, label, spent_on,"
            " amount) values (%s,'Conference','2026-09-01',450000)",
            (org_a,))
        contact = c.execute(
            "insert into public.contacts (org_id, customer_id, name)"
            " values (%s,%s,'Dr Farooq') returning id",
            (org_a, cust)).fetchone()[0]
        c.execute(
            "insert into public.interactions (org_id, customer_id,"
            " contact_id, happened_on, summary, created_by)"
            " values (%s,%s,%s,'2026-11-01','Discussed the tender',%s)",
            (org_a, cust, contact, anwar))

    tables = ("accounts", "equity_movements", "commitments",
              "cash_balances", "employees", "opportunities",
              "marketing_spend", "contacts", "interactions")

    with as_user(db, anwar):
        mine = {t: db.execute(
            f"select count(*) from public.{t}").fetchone()[0]
            for t in tables}
    assert all(n > 0 for n in mine.values()), \
        f"the owner cannot see their own rows: {mine}"

    with as_user(db, bilal):
        theirs = {t: db.execute(
            f"select count(*) from public.{t}").fetchone()[0]
            for t in tables}
    leaked = {t: n for t, n in theirs.items() if n}
    assert not leaked, f"another importer can read: {leaked}"


def test_a_stranger_cannot_write_into_somebody_elses_books(db):
    """Reading is half of it. A policy that refuses a select and
    allows an insert lets somebody put a liability in another
    company's accounts.
    """
    anwar = new_user(db, f"own-{uuid.uuid4().hex[:6]}@a.test")
    outsider = new_user(db, f"out-{uuid.uuid4().hex[:6]}@z.test")
    org_a, _loc = new_org(db, anwar, "Importer A")

    # One transaction per attempt. A refused statement aborts its
    # transaction, so a second attempt inside the same block fails
    # with "current transaction is aborted" -- which looks like a pass
    # and proves nothing about the policy.
    attempts = (
        ("insert into public.accounts (org_id, code, name, line,"
         " period_from, period_to) values (%s,'9999','Theirs',"
         "'payables','2026-01-01','2026-12-31')", (org_a,)),
        ("insert into public.commitments (org_id, label, kind,"
         " due_date, amount) values (%s,'Fake','other',"
         "'2026-12-01',1)", (org_a,)),
        ("insert into public.employees (org_id, employee_no, name,"
         " basic_monthly) values (%s,'X-1','Nobody',1)", (org_a,)),
    )
    for sql, args in attempts:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with as_user(db, outsider) as c:
                c.execute(sql, args)

    # And nothing landed.
    with as_user(db, anwar) as c:
        for table in ("accounts", "commitments", "employees"):
            assert c.execute(
                f"select count(*) from public.{table}").fetchone()[0] == 0, (
                f"an outsider wrote into {table}")


# ------------------------------------------- one month, one source
#
# demand_monthly was a UNION ALL of ledger sales and imported Sales
# history, so a business that imported its history and then recorded
# sales in the app had both for the same month and the engine summed
# them. Demand feeds the forecast, the forecast feeds the reorder
# point, and the reorder point feeds the order quantity -- so the
# product told an importer to buy twice what they needed.


def test_a_month_in_both_the_ledger_and_the_import_is_counted_once(db):
    owner = new_user(db, f"demand-{uuid.uuid4().hex[:6]}@t.test")
    org, loc = new_org(db, owner, "Demand Co")
    this_month = date.today().strftime("%Y-%m")

    with as_user(db, owner):
        item = db.execute(
            "insert into public.items (org_id, sku, description,"
            " lead_time_days) values (%s,%s,'Reagent',30) returning id",
            (org, sku("DM")),
        ).fetchone()[0]
        # They import their history: 100 sold this month.
        db.execute(
            "insert into public.demand_history (org_id, location_id,"
            " item_id, period, quantity) values (%s,%s,%s,%s,100)",
            (org, loc, item, this_month))
        # Then record the very same month's sales in the app.
        db.execute(
            "insert into public.stock_movement (org_id, item_id,"
            " location_id, qty, unit_cost, reason, occurred_at)"
            " values (%s,%s,%s,500,100,'opening',current_date)",
            (org, item, loc))
        db.execute(
            "insert into public.stock_movement (org_id, item_id,"
            " location_id, qty, reason, occurred_at)"
            " values (%s,%s,%s,-100,'sale',current_date)",
            (org, item, loc))

        rows = db.execute(
            "select source, quantity from public.demand_monthly"
            " where item_id = %s and period = %s", (item, this_month)
        ).fetchall()

    assert len(rows) == 1, f"the same month twice: {rows}"
    assert float(rows[0][1]) == 100.0, "they sold 100, not 200"
    # The fault this test was written for is the 200. The source moved
    # from the ledger to history when 0017 narrowed the rule: this is
    # the month the branch went live, where the ledger holds the days
    # since they began and the import holds the whole month. Both say
    # 100 here, which is why the count is unaffected.
    assert rows[0][0] == "import", rows


def test_imported_history_still_fills_the_months_before_the_ledger(db):
    """The import is a backfill. Preferring the ledger must not throw
    away the history for months it does not cover, or every business
    loses its past the day it records its first sale."""
    owner = new_user(db, f"backfill-{uuid.uuid4().hex[:6]}@t.test")
    org, loc = new_org(db, owner, "Backfill Co")
    today = date.today()
    this_month = today.strftime("%Y-%m")
    older = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    with as_user(db, owner):
        item = db.execute(
            "insert into public.items (org_id, sku, description,"
            " lead_time_days) values (%s,%s,'Reagent',30) returning id",
            (org, sku("BF")),
        ).fetchone()[0]
        for period, qty in ((older, 80), (this_month, 100)):
            db.execute(
                "insert into public.demand_history (org_id, location_id,"
                " item_id, period, quantity) values (%s,%s,%s,%s,%s)",
                (org, loc, item, period, qty))
        db.execute(
            "insert into public.stock_movement (org_id, item_id,"
            " location_id, qty, unit_cost, reason, occurred_at)"
            " values (%s,%s,%s,500,100,'opening',current_date)",
            (org, item, loc))
        db.execute(
            "insert into public.stock_movement (org_id, item_id,"
            " location_id, qty, reason, occurred_at)"
            " values (%s,%s,%s,-40,'sale',current_date)",
            (org, item, loc))

        got = dict(db.execute(
            "select period, source from public.demand_monthly"
            " where item_id = %s", (item,)).fetchall())

    assert got[older] == "import", "the month before the ledger was lost"
    # Also the opening month, so the import's whole month stands rather
    # than the ledger's part of it -- 0017. The backfill this test is
    # about is the older month, and it is still there.
    assert got[this_month] == "import", got


# ------------------------------------------ one organisation per person
#
# The policies always allowed a user to belong to several, and the
# application was not ready for it. Proven on a clean database: one
# user, two importers, each with SKU RGT-100 at branch KHI, one
# selling 50 a month and the other 30. The engine reads 80 for both,
# because DEMAND_SQL groups by SKU, branch and month with no
# organisation in the key. A forecast drives the reorder point, which
# drives what the product tells somebody to buy.


def test_a_second_organisation_is_refused(db):
    owner = new_user(db, f"two-{uuid.uuid4().hex[:6]}@t.test")
    new_org(db, owner, "First Importer")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with as_user(db, owner):
            db.execute(
                "insert into public.organisations (name, created_by)"
                " values ('Second Importer', %s)", (owner,))


def test_the_restriction_is_about_active_membership(db):
    """Somebody suspended from an old employer has not used up their
    one organisation. The index is partial on status for that reason,
    and `suspended` is the only other value the column allows."""
    owner = new_user(db, f"moved-{uuid.uuid4().hex[:6]}@t.test")
    org, _ = new_org(db, owner, "Old Employer")
    db.execute(
        "update public.memberships set status = 'suspended'"
        " where user_id = %s and org_id = %s", (owner, org))
    with as_user(db, owner):
        db.execute(
            "insert into public.organisations (name, created_by)"
            " values ('New Employer', %s)", (owner,))
        n = db.execute(
            "select count(*) from public.memberships"
            " where user_id = %s and status = 'active'", (owner,)).fetchone()[0]
    assert n == 1


def test_two_organisations_cannot_pool_their_demand(db):
    """The arithmetic the restriction protects.

    Built the only way it still can be: two owners, one organisation
    each, same SKU and branch code. Each has to see its own figure and
    not the sum.
    """
    this_month = date.today().strftime("%Y-%m")
    seen = []
    for name, sold in (("Importer One", 50), ("Importer Two", 30)):
        owner = new_user(db, f"pool-{uuid.uuid4().hex[:6]}@t.test")
        org, loc = new_org(db, owner, name)
        with as_user(db, owner):
            item = db.execute(
                "insert into public.items (org_id, sku, description,"
                " lead_time_days) values (%s,'RGT-100','Reagent',30)"
                " returning id", (org,)).fetchone()[0]
            db.execute(
                "insert into public.demand_history (org_id, location_id,"
                " item_id, period, quantity) values (%s,%s,%s,%s,%s)",
                (org, loc, item, this_month, sold))
            got = db.execute(
                "select sum(quantity) from public.demand_monthly"
                " where period = %s", (this_month,)).fetchone()[0]
        seen.append(float(got))

    assert seen == [50.0, 30.0], \
        f"one business is reading another's sales: {seen}"
