"""What changed, who changed it, and what it was before.

An item's lead time can go from 120 days to 12 in one keystroke on
the stock grid. That figure feeds the reorder point, which feeds
the order quantity, which feeds the cash plan -- so the whole
business buys differently from that moment, and until now nothing
anywhere recorded that it had happened, let alone what the number
used to be.

The only audit the product had was for shipment weights and volumes
(0019) and for corrected receipt lines, which are reversals and
carry their own history. Those were written because they move money
between items. So does a lead time, a credit limit and a unit
price; they just do it more slowly.

A trigger rather than application code, for the reason the
append-only ledger is a trigger: the rule then holds for every path
into the table -- the endpoint, the importer, a service-role script,
somebody in the SQL console -- and not only for the path somebody
remembered to instrument.

Scope is UPDATE and DELETE, the two that destroy information. A row
being created is visible as the row, and the tables that matter
carry created_at.
"""

from __future__ import annotations

import os
import pathlib
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("LANDED_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="LANDED_TEST_DSN not set")

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def db():
    con = psycopg.connect(DSN, autocommit=True)
    for s in ("public", "app", "auth"):
        con.execute(f"drop schema if exists {s} cascade")
    con.execute("create schema public")
    shim = ROOT / "supabase" / "tests" / "00_supabase_shim.sql"
    for path in [shim,
                 *sorted((ROOT / "supabase" / "migrations").glob("*.sql"))]:
        con.execute(path.read_text())
    yield con
    con.close()


@pytest.fixture
def org(db):
    """A fresh organisation with an owner, acting as that owner."""
    who = uuid.uuid4()
    db.execute("insert into auth.users (id, email) values (%s,%s)",
               (who, f"{who}@audit.test"))
    db.execute("delete from public.organisations")
    org_id = db.execute(
        "insert into public.organisations (name, created_by)"
        " values ('Audit Co', %s) returning id", (who,)).fetchone()[0]
    db.execute(
        "insert into public.memberships (org_id, user_id, role)"
        " values (%s,%s,'owner') on conflict do nothing", (org_id, who))
    return org_id, who


def as_user(db, who):
    """Run the next statements as that signed-in person.

    Session-scoped, not transaction-scoped. The connection is
    autocommit, so a `set local` is discarded the moment the
    statement that set it finishes -- which made auth.uid() null and
    the role reset, and the two tests that depend on either quietly
    tested nothing.
    """
    db.execute("select set_config('request.jwt.claims', %s, false)",
               ('{"sub": "%s", "role": "authenticated"}' % who,))


def as_nobody(db):
    db.execute("select set_config('request.jwt.claims', '', false)")


def changes(db, **where):
    sql = ("select table_name, record_label, field, was, became,"
           "       changed_by is not null"
           "  from public.record_changes")
    args = []
    if where:
        sql += " where " + " and ".join(f"{k} = %s" for k in where)
        args = list(where.values())
    sql += " order by changed_at, field"
    return db.execute(sql, tuple(args)).fetchall()


# ------------------------------------------------------ the figures


def test_a_changed_lead_time_is_recorded_with_what_it_was(db, org):
    org_id, who = org
    db.execute("insert into public.items (org_id, sku, description,"
               " lead_time_days) values (%s,'RGT-1','Reagent',120)",
               (org_id,))
    db.execute("delete from public.record_changes")
    db.execute("update public.items set lead_time_days = 12"
               " where sku = 'RGT-1'")
    got = changes(db, field="lead_time_days")
    assert len(got) == 1
    table, label, field, was, became, _by = got[0]
    assert (table, label, field) == ("items", "RGT-1", "lead_time_days")
    # As the column stores it, scale and all. An audit records the
    # value, not a rendering of it; the screen can format.
    assert (was, became) == ("120.00", "12.00")


def test_only_the_fields_that_moved_are_recorded(db, org):
    """A row updated in full writes one line per changed column, not
    one per column. An audit that lists every field of every edit is
    one nobody reads."""
    org_id, _who = org
    db.execute("insert into public.items (org_id, sku, description,"
               " lead_time_days, list_price)"
               " values (%s,'RGT-2','Buffer',30,100)", (org_id,))
    db.execute("delete from public.record_changes")
    db.execute("update public.items set description = 'Buffer solution',"
               " lead_time_days = 30, list_price = 140 where sku = 'RGT-2'")
    fields = {c[2] for c in changes(db)}
    assert fields == {"description", "list_price"}, (
        "lead_time_days did not move and should not be listed")


def test_an_update_that_changes_nothing_records_nothing(db, org):
    org_id, _who = org
    db.execute("insert into public.items (org_id, sku, description)"
               " values (%s,'RGT-3','Probe')", (org_id,))
    db.execute("delete from public.record_changes")
    db.execute("update public.items set description = 'Probe'"
               " where sku = 'RGT-3'")
    assert changes(db) == []


def test_the_person_who_changed_it_is_recorded(db, org):
    org_id, who = org
    db.execute("insert into public.items (org_id, sku, description,"
               " lead_time_days) values (%s,'RGT-4','Tip',10)", (org_id,))
    db.execute("delete from public.record_changes")
    as_user(db, who)
    db.execute("update public.items set lead_time_days = 20"
               " where sku = 'RGT-4'")
    as_nobody(db)
    got = changes(db, field="lead_time_days")
    assert got and got[0][5] is True, "nobody is recorded as having done it"


def test_a_deleted_record_is_recorded_before_it_goes(db, org):
    """The one case where the row itself cannot be consulted
    afterwards, which is exactly when somebody wants to know."""
    org_id, _who = org
    db.execute("insert into public.suppliers (org_id, code, name)"
               " values (%s,'ACME','Acme Diagnostics')", (org_id,))
    db.execute("delete from public.record_changes")
    db.execute("delete from public.suppliers where code = 'ACME'")
    got = changes(db)
    assert got, "a deleted supplier left no trace"
    assert got[0][0] == "suppliers"
    assert got[0][1] == "ACME"
    assert got[0][2] == "(deleted)"
    assert "Acme Diagnostics" in (got[0][3] or "")


def test_a_null_becoming_a_value_reads_as_a_change(db, org):
    org_id, _who = org
    db.execute("insert into public.items (org_id, sku, description)"
               " values (%s,'RGT-5','Vial')", (org_id,))
    db.execute("delete from public.record_changes")
    db.execute("update public.items set list_price = 900"
               " where sku = 'RGT-5'")
    got = changes(db, field="list_price")
    assert len(got) == 1
    assert got[0][3] is None and got[0][4] == "900.00"


# -------------------------------------------- the rest of the tables


@pytest.mark.parametrize("table,label_col,label,field,before,after", [
    ("suppliers", "code", "SUP-1", "lead_time_days", "40.00", "70.00"),
    ("customers", "code", "CUS-1", "credit_limit",
     "100000.00", "250000.00"),
    ("locations", "code", "KHI", "name", "Karachi", "Karachi Main"),
])
def test_every_watched_table_records_its_changes(
        db, org, table, label_col, label, field, before, after):
    org_id, _who = org
    cols = {"suppliers": "(org_id, code, name, lead_time_days)"
                         " values (%s,%s,'A',40)",
            "customers": "(org_id, code, name, credit_limit)"
                         " values (%s,%s,'A',100000)",
            "locations": "(org_id, code, name) values (%s,%s,'Karachi')"}
    db.execute(f"insert into public.{table} {cols[table]}", (org_id, label))
    db.execute("delete from public.record_changes")
    db.execute(f"update public.{table} set {field} = %s"
               f" where {label_col} = %s", (after, label))
    got = changes(db, field=field)
    assert len(got) == 1, f"{table}.{field} is not being watched"
    assert got[0][1] == label
    assert (got[0][3], got[0][4]) == (before, after)


def test_the_ledger_is_not_watched(db, org):
    """stock_movement already refuses update and delete by trigger,
    so a second trigger watching for them is dead weight that reads
    as though the ledger could be edited."""
    got = db.execute(
        "select count(*) from pg_trigger t"
        "  join pg_class c on c.oid = t.tgrelid"
        " where c.relname = 'stock_movement'"
        "   and t.tgname like '%record_change%'").fetchone()[0]
    assert got == 0


def test_one_organisation_cannot_read_anothers_history(db, org):
    """The audit carries field values, so a leak here is a leak of
    everything the audit covers."""
    org_id, _who = org
    other = uuid.uuid4()
    db.execute("insert into auth.users (id, email) values (%s,%s)",
               (other, f"{other}@audit.test"))
    db.execute("insert into public.items (org_id, sku, description,"
               " lead_time_days) values (%s,'SECRET-1','x',5)", (org_id,))
    db.execute("update public.items set lead_time_days = 9"
               " where sku = 'SECRET-1'")

    db.execute("set role authenticated")
    as_user(db, other)
    seen = db.execute("select count(*) from public.record_changes"
                      ).fetchone()[0]
    db.execute("reset role")
    as_nobody(db)
    assert seen == 0, "another organisation can read the audit history"


# ------------------------------------- how it reads back on a screen
#
# Read from the source: the drawer's rendering has no endpoint of
# its own. Driven in Chromium against a real database, changing a
# lead time through /api/item and reading it back as "TYPED OVER /
# lead time / 120 -> 12".

APP = (ROOT / "web" / "static" / "app.js").read_text()


def test_a_column_name_is_shown_the_way_a_person_says_it():
    """said() turns an underscored name into words, which gets
    "lead time days" and "is clinical critical" -- readable, and not
    what anybody calls them."""
    i = APP.index("const SAID = {")
    block = APP[i:APP.index("};", i)]
    for name, shown in (("lead_time_days", "lead time"),
                        ("is_clinical_critical", "clinically critical"),
                        ("allocate_by", "divided by"),
                        ("credit_limit", "credit limit")):
        assert f"{name}: '{shown}'" in block, f"{name} reads badly"


def test_a_stored_value_is_shown_without_its_scale():
    """The audit keeps what the column held, because an audit
    records the value and not a rendering of it. "120.00" days is
    still an odd thing to show somebody."""
    assert "function plainly(" in APP, "no display rule for stored values"
    i = APP.index("function plainly(")
    body = APP[i:APP.index("\n}", i)]
    assert "'yes'" in body and "'no'" in body, (
        "a stored boolean still reads as 'true'")
    assert "toFixed" in body, "a stored 120.00 still reads as 120.00"


def test_the_edits_are_kept_apart_from_the_movements():
    """They answer different questions, and one list reads as though
    an edit were a kind of movement."""
    i = APP.index("if (edits.length) {")
    assert "'Typed over'" in APP[i:i + 300]
