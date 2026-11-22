"""Reading a real workbook into a real database.

Both halves are real on purpose. The sheets come from
tools/make_import_workbook.py, so the thing being parsed is the thing
customers are handed; the database is a real Postgres with the real
migrations, so the policies, the constraints and the append-only
trigger are all in force.

A mock on either side would have passed while the real pair failed --
which is how four non-existent column names survived a careful read of
the SQL earlier in this project.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from engine.importer import BadWorkbook, Plan, apply, plan

DSN = os.environ.get("LANDED_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="no LANDED_TEST_DSN")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def book(tmp_path_factory) -> bytes:
    out = tmp_path_factory.mktemp("wb")
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_import_workbook.py"),
                    str(out)], check=True, cwd=ROOT)
    f = next(out.glob("landed-import-meridian*.xlsx"))
    return f.read_bytes()


@pytest.fixture
def org():
    """An empty organisation, and a connection acting as its owner."""
    conn = psycopg.connect(DSN)
    cur = conn.cursor()
    email = f"imp-{uuid.uuid4()}@example.test"
    cur.execute("insert into auth.users (id, email) values "
                "(gen_random_uuid(), %s) returning id", (email,))
    owner = cur.fetchone()[0]
    cur.execute("insert into public.organisations (name, created_by) "
                "values ('Import Co', %s) returning id", (owner,))
    org_id = cur.fetchone()[0]
    conn.commit()

    cur.execute("set local role authenticated")
    cur.execute("select set_config('request.jwt.claims', %s, true)",
                ('{"sub": "%s", "role": "authenticated"}' % owner,))
    yield conn, cur, org_id

    conn.rollback()
    cur.execute("reset role")
    cur.execute("alter table public.stock_movement disable trigger all")
    cur.execute("delete from public.organisations where id = %s", (org_id,))
    cur.execute("delete from auth.users where id = %s", (owner,))
    cur.execute("alter table public.stock_movement enable trigger all")
    conn.commit()
    conn.close()


# ------------------------------------------------------------- reading

def test_a_real_workbook_reads_without_problems(book):
    p = plan(book)
    assert p.ok, [x.message for x in p.fatal][:5]
    assert p.counts_sane if hasattr(p, "counts_sane") else True
    d = p.as_dict()
    assert d["counts"]["branches"] == 4
    assert d["counts"]["items"] == 60
    assert d["counts"]["stock"] > 150
    assert d["counts"]["history"] == d["counts"]["stock"] * 24
    assert d["months"] == [p.months[0], p.months[-1]]


def test_the_footnote_under_the_data_is_not_read_as_a_row(book):
    """Every sheet carries a line of advice below the last row. Reading
    to max_row hands the importer a SKU that is a sentence."""
    p = plan(book)
    for i in p.items:
        assert len(i["sku"]) < 40, i["sku"]
    for s in p.stock:
        assert len(s["sku"]) < 40, s["sku"]


def test_a_file_that_is_not_a_workbook_is_refused_clearly():
    with pytest.raises(BadWorkbook):
        plan(b"this is not a spreadsheet")


def test_missing_required_sheets_are_named(book):
    from io import BytesIO
    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(book))
    del wb["Stock"]
    buf = BytesIO()
    wb.save(buf)
    p = plan(buf.getvalue())
    assert not p.ok
    assert "Stock" in p.fatal[0].message


def test_a_sku_on_stock_but_not_on_items_is_fatal(book):
    """The commonest real import error, and the worst silent outcome:
    skipped quietly, the business's stock total is wrong and nothing
    on the screen says so."""
    from io import BytesIO
    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(book))
    wb["Stock"].cell(2, 1, "GHOST-9999")
    buf = BytesIO()
    wb.save(buf)
    p = plan(buf.getvalue())
    assert not p.ok
    assert any("GHOST-9999" in x.message for x in p.fatal)


def test_a_duplicate_sku_on_items_is_fatal(book):
    from io import BytesIO
    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(book))
    first = wb["Items"].cell(2, 1).value
    wb["Items"].cell(3, 1, first)
    buf = BytesIO()
    wb.save(buf)
    p = plan(buf.getvalue())
    assert not p.ok
    assert any("more than once" in x.message for x in p.fatal)


def test_negative_stock_is_refused(book):
    from io import BytesIO
    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(book))
    wb["Stock"].cell(2, 3, -5)
    buf = BytesIO()
    wb.save(buf)
    p = plan(buf.getvalue())
    assert any("negative" in x.message for x in p.problems)


def test_a_blank_month_is_a_zero_not_a_missing_row(book):
    from io import BytesIO
    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(book))
    wb["Sales history"].cell(2, 5, None)
    buf = BytesIO()
    wb.save(buf)
    p = plan(buf.getvalue())
    sku = wb["Sales history"].cell(2, 1).value
    branch = wb["Sales history"].cell(2, 2).value
    rows = [h for h in p.history if h["sku"] == sku and h["branch"] == branch]
    assert len(rows) == 24
    assert sum(1 for r in rows if r["qty"] == 0) >= 1


# ------------------------------------------------------------- writing

def test_the_whole_workbook_writes_and_reads_back(book, org):
    """End to end: workbook in, engine-ready snapshot out."""
    conn, cur, _ = org
    p = plan(book)
    made = apply(cur, p)
    assert made["branches"] == 4
    assert made["items"] == 60
    assert made["movements"] > 100

    cur.execute("select count(*) from public.locations")
    assert cur.fetchone()[0] == 4
    cur.execute("select count(*) from public.items")
    assert cur.fetchone()[0] == 60

    from engine.db.snapshot import read

    snap = read(cur, date.today())
    assert not snap.is_empty
    assert len(snap.items) == len(p.stock)
    assert sorted(snap.branches) == ["FSD", "ISB", "KHI", "LHR"]

    # The figures that came out are the figures that went in.
    by = {(i.item_key, i.location_id): i for i in snap.items}
    for s in p.stock[:20]:
        if s["qty"]:
            got = by[(s["sku"], s["branch"])]
            assert got.on_hand == pytest.approx(s["qty"])


def test_stock_arrives_as_a_ledger_entry_not_a_balance(book, org):
    """There is no balance column anywhere, deliberately: a stored
    balance is a second number that can disagree with the movements
    behind it, and nobody can then tell which is wrong."""
    conn, cur, _ = org
    apply(cur, plan(book))
    cur.execute("select count(*) from public.stock_movement "
                "where reason = 'opening'")
    opening = cur.fetchone()[0]
    assert opening > 100
    cur.execute("select count(*) from public.stock_movement "
                "where reason <> 'opening'")
    assert cur.fetchone()[0] == 0


def test_importing_the_same_file_twice_does_not_double_the_stock(book, org):
    """Somebody will click it twice, or re-upload a corrected file.

    Items and branches are matched on their natural keys, and the
    opening movement carries a client_ref so the ledger can refuse the
    replay rather than quietly adding the stock again.
    """
    conn, cur, org_id = org
    p = plan(book)
    apply(cur, p)
    conn.commit()

    # Counted within this organisation, not across the database. These
    # assertions used to be unscoped, which made the test pass only
    # while the database held exactly one organisation's rows -- so it
    # went green on its own and red whenever a module that seeds or
    # rebuilds the schema had run first. The thing being tested is a
    # replay into one tenant, so that is what is counted.
    cur.execute("select sum(qty) from public.stock_movement"
                " where org_id = %s", (org_id,))
    first = cur.fetchone()[0]

    try:
        apply(cur, plan(book))
        conn.commit()
    except Exception:
        conn.rollback()

    cur.execute("select count(*) from public.items where org_id = %s",
                (org_id,))
    assert cur.fetchone()[0] == 60, "items were duplicated"
    cur.execute("select sum(qty) from public.stock_movement"
                " where org_id = %s", (org_id,))
    assert cur.fetchone()[0] == first, "the opening stock was counted twice"


def test_nothing_is_written_when_the_plan_has_problems(book, org):
    conn, cur, _ = org
    p = Plan()
    from engine.importer import Problem
    p.problems.append(Problem("Items", 2, "broken"))
    with pytest.raises(BadWorkbook):
        apply(cur, p)
    cur.execute("select count(*) from public.items")
    assert cur.fetchone()[0] == 0


def test_the_commit_does_not_send_a_statement_per_row(book, org):
    """The failure that reached production.

    One statement at a time this sent 5,218 of them. Over a unix socket
    in this test that is a couple of seconds, so every test passed;
    across the network to a hosted database it is two to five minutes,
    and the platform cuts the request and returns 502 with no body.

    Counting round trips rather than timing them, because the clock
    here is the wrong clock -- it is the network that makes this fail,
    and the test machine has none.
    """
    conn, cur, _ = org
    p = plan(book)
    rows = len(p.stock) * 2 + len(p.history) + len(p.orders)
    assert rows > 4000, "the sample is too small to be a fair test"

    class Counting:
        def __init__(self, real):
            self._real = real
            self.executes = 0
            self.batches = 0

        def execute(self, *a, **k):
            self.executes += 1
            return self._real.execute(*a, **k)

        def executemany(self, *a, **k):
            self.batches += 1
            return self._real.executemany(*a, **k)

        def __getattr__(self, name):
            return getattr(self._real, name)

    spy = Counting(cur)
    apply(spy, p)

    # Branches, suppliers, items and PO headers are one each and need
    # their ids back. Everything else is batched.
    assert spy.executes < 200, (
        f"{spy.executes} individual statements for {rows} rows -- this is "
        f"the shape that times out against a real database")
    assert spy.batches >= 3, "the bulk tables are not being batched"


def test_every_row_still_arrives_when_batched(book, org):
    """Speed is worthless if it drops rows. Counted against the plan."""
    conn, cur, _ = org
    p = plan(book)
    apply(cur, p)

    cur.execute("select count(*) from public.demand_history")
    assert cur.fetchone()[0] == len(p.history)
    cur.execute("select count(*) from public.stock_movement")
    assert cur.fetchone()[0] == sum(1 for s in p.stock if s["qty"])
    cur.execute("select count(*) from public.item_locations")
    assert cur.fetchone()[0] == len(p.stock)
    cur.execute("select count(*) from public.po_lines")
    assert cur.fetchone()[0] == len(p.orders)


def test_the_workbook_brings_in_customers_machines_and_jobs(book, org):
    """Four more sheets, written into the tables the other three engine
    modules read. The workbook is the only way these get in, so if it
    does not carry them those screens are permanently empty."""
    conn, cur, _ = org
    p = plan(book)
    assert p.customers and p.invoices and p.machines and p.jobs

    made = apply(cur, p)
    assert made["customers"] == len(p.customers)
    assert made["invoices"] == len(p.invoices)
    assert made["machines"] == len(p.machines)
    assert made["service_jobs"] == len(p.jobs)

    from engine.db.snapshot import read_invoices, read_jobs, read_machines

    assert len(read_machines(cur)) == len(p.machines)
    assert len(read_jobs(cur)) == len(p.jobs)
    # Only the unpaid ones are owed, so the reader returns every
    # invoice and the engine decides which still count.
    assert len(read_invoices(cur)) == len(p.invoices)


def test_an_invoice_paid_more_than_it_was_worth_is_refused(book):
    """Not clamped. More paid than invoiced is a real mistake somewhere
    and flooring it silently hides which."""
    from io import BytesIO

    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(book))
    ws = wb["Invoices"]
    ws.cell(2, 6, 100)      # amount
    ws.cell(2, 7, 500)      # paid
    buf = BytesIO()
    wb.save(buf)
    p = plan(buf.getvalue())
    assert not p.ok
    assert any("more than the invoice" in x.message for x in p.fatal)


def test_an_invoice_for_an_unknown_customer_is_fatal(book):
    from io import BytesIO

    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(book))
    wb["Invoices"].cell(2, 2, "GHOST")
    buf = BytesIO()
    wb.save(buf)
    p = plan(buf.getvalue())
    assert not p.ok
    assert any("GHOST" in x.message for x in p.fatal)
