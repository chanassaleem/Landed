"""The four things that happen to stock in a day.

Against a real Postgres, because the ledger's append-only trigger and
its policies are part of what is being tested: a correction here is a
new row, never an edit, and the database is what enforces that.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import pytest

psycopg = pytest.importorskip("psycopg")

from engine.operations import NotAllowed, adjust, history, record, transfer

DSN = os.environ.get("LANDED_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="no LANDED_TEST_DSN")


@pytest.fixture
def shop():
    """One item, two branches, 100 on the shelf at KHI."""
    conn = psycopg.connect(DSN)
    cur = conn.cursor()
    cur.execute("insert into auth.users (id, email) values "
                "(gen_random_uuid(), %s) returning id",
                (f"ops-{uuid.uuid4()}@example.test",))
    owner = cur.fetchone()[0]
    cur.execute("insert into public.organisations (name, created_by) "
                "values ('Ops Co', %s) returning id", (owner,))
    org = cur.fetchone()[0]
    for code, name in (("KHI", "Karachi"), ("LHR", "Lahore")):
        cur.execute("insert into public.locations (org_id, code, name) "
                    "values (%s, %s, %s)", (org, code, name))
    cur.execute("insert into public.items (org_id, sku, description, "
                "lead_time_days) values (%s,'RGT-1','Reagent pack',30) "
                "returning id", (org,))
    item = cur.fetchone()[0]
    cur.execute("select id from public.locations where org_id = %s and "
                "code = 'KHI'", (org,))
    khi = cur.fetchone()[0]
    cur.execute("insert into public.stock_movement (org_id, item_id, "
                "location_id, qty, unit_cost, reason, occurred_at) "
                "values (%s,%s,%s,100,500,'opening',%s)",
                (org, item, khi, date.today() - timedelta(days=30)))
    conn.commit()

    cur.execute("set local role authenticated")
    cur.execute("select set_config('request.jwt.claims', %s, true)",
                ('{"sub": "%s", "role": "authenticated"}' % owner,))
    yield conn, cur

    conn.rollback()
    cur.execute("reset role")
    cur.execute("alter table public.stock_movement disable trigger all")
    cur.execute("delete from public.organisations where id = %s", (org,))
    cur.execute("delete from auth.users where id = %s", (owner,))
    cur.execute("alter table public.stock_movement enable trigger all")
    conn.commit()
    conn.close()


def on_hand(cur, branch="KHI"):
    return float(cur.execute(
        "select coalesce(sum(m.qty), 0) from public.stock_movement m "
        "join public.locations l on l.id = m.location_id "
        "where l.code = %s", (branch,)).fetchone()[0])


# ----------------------------------------------------------- the basics

def test_receiving_adds_to_the_shelf(shop):
    _, cur = shop
    record(cur, "RGT-1", "KHI", "grn", 40, unit_cost=520)
    assert on_hand(cur) == 140


def test_selling_takes_away(shop):
    _, cur = shop
    record(cur, "RGT-1", "KHI", "sale", 30)
    assert on_hand(cur) == 70


def test_the_sign_comes_from_the_kind_not_the_caller(shop):
    """A form that could send a negative sale would make the ledger
    wrong by twice the quantity, with nothing out of place to notice."""
    _, cur = shop
    with pytest.raises(NotAllowed):
        record(cur, "RGT-1", "KHI", "sale", -30)
    assert on_hand(cur) == 100


def test_you_cannot_sell_more_than_is_there(shop):
    """A negative shelf is always a mistake somewhere -- usually an
    unrecorded receipt -- and letting it through hides which."""
    _, cur = shop
    with pytest.raises(NotAllowed) as e:
        record(cur, "RGT-1", "KHI", "sale", 500)
    assert "only 100" in str(e.value)
    assert "Adjust count" in str(e.value)
    assert on_hand(cur) == 100


def test_an_unknown_line_is_refused_by_name(shop):
    _, cur = shop
    with pytest.raises(NotAllowed) as e:
        record(cur, "NOPE-1", "KHI", "sale", 1)
    assert "NOPE-1" in str(e.value)


def test_a_reason_the_ledger_does_not_know_is_refused(shop):
    _, cur = shop
    with pytest.raises(NotAllowed):
        record(cur, "RGT-1", "KHI", "stolen_by_pirates", 1)


# -------------------------------------------------------- stock takes

def test_an_adjustment_takes_the_count_not_the_difference(shop):
    """Asking for a difference asks somebody to do arithmetic against a
    number they cannot see, and get the sign right as well. That is how
    a count of 48 becomes a correction of minus 48."""
    _, cur = shop
    out = adjust(cur, "RGT-1", "KHI", counted=92)
    assert out["was"] == 100 and out["now"] == 92
    assert out["qty"] == -8
    assert on_hand(cur) == 92


def test_an_adjustment_upwards_works_too(shop):
    _, cur = shop
    adjust(cur, "RGT-1", "KHI", counted=118)
    assert on_hand(cur) == 118


def test_an_adjustment_that_agrees_writes_nothing(shop):
    _, cur = shop
    before = cur.execute(
        "select count(*) from public.stock_movement").fetchone()[0]
    out = adjust(cur, "RGT-1", "KHI", counted=100)
    assert out["qty"] == 0
    after = cur.execute(
        "select count(*) from public.stock_movement").fetchone()[0]
    assert after == before


def test_a_correction_is_a_new_row_never_an_edit(shop):
    """The ledger is append-only and the database enforces it. What you
    believed last week is still there, which is what makes a stock
    figure auditable rather than merely current."""
    _, cur = shop
    adjust(cur, "RGT-1", "KHI", counted=92)
    rows = cur.execute(
        "select count(*) from public.stock_movement").fetchone()[0]
    assert rows == 2            # the opening, and the correction

    # An UPDATE does not raise -- there is no update policy, so
    # row-level security filters every row out and the statement
    # changes nothing. Silent, but that is the schema working: the
    # trigger is the second line, for anyone who gets past the first.
    cur.execute("update public.stock_movement set qty = 1")
    assert cur.rowcount == 0
    assert on_hand(cur) == 92


# ----------------------------------------------------------- transfers

def test_a_transfer_moves_both_ways_at_once(shop):
    _, cur = shop
    transfer(cur, "RGT-1", "KHI", "LHR", 25)
    assert on_hand(cur, "KHI") == 75
    assert on_hand(cur, "LHR") == 25


def test_a_transfer_leaves_the_total_alone(shop):
    _, cur = shop
    transfer(cur, "RGT-1", "KHI", "LHR", 25)
    total = float(cur.execute(
        "select sum(qty) from public.stock_movement").fetchone()[0])
    assert total == 100


def test_a_transfer_of_more_than_is_there_is_refused(shop):
    _, cur = shop
    with pytest.raises(NotAllowed):
        transfer(cur, "RGT-1", "KHI", "LHR", 500)
    assert on_hand(cur, "KHI") == 100
    assert on_hand(cur, "LHR") == 0


def test_a_transfer_to_the_same_branch_is_refused(shop):
    _, cur = shop
    with pytest.raises(NotAllowed):
        transfer(cur, "RGT-1", "KHI", "KHI", 5)


# ------------------------------------------------------------- history

def test_history_reads_back_most_recent_first(shop):
    _, cur = shop
    record(cur, "RGT-1", "KHI", "grn", 40, unit_cost=520)
    record(cur, "RGT-1", "KHI", "sale", 10)
    rows = history(cur, "RGT-1", "KHI")
    assert [r["reason"] for r in rows][:2] == ["sale", "grn"]
    assert rows[-1]["reason"] == "opening"
    assert sum(r["qty"] for r in rows) == on_hand(cur)


# --------------------------------------------- a receipt needs a cost
#
# The kind knew it wanted a cost and nothing checked. The movement
# went in with a null unit_cost; public.item_cost skips null-cost
# movements when it averages and the on-hand figure does not, so units
# received without a price were valued at the price of whatever else
# was on the shelf.


def test_a_receipt_without_a_cost_is_refused(shop):
    _, cur = shop
    with pytest.raises(NotAllowed) as e:
        record(cur, "RGT-1", "KHI", "grn", 100)
    assert "cost" in str(e.value).lower()
    assert "0" in str(e.value), "it does not say what to do for a free one"


def test_a_cost_of_zero_is_a_cost(shop):
    """A free sample and a warranty replacement are both real, and both
    are stated. What is refused is silence, not zero."""
    _, cur = shop
    record(cur, "RGT-1", "KHI", "grn", 10, unit_cost=0)
    row = cur.execute(
        "select unit_cost from public.stock_movement"
        " where reason = 'grn' order by created_at desc limit 1").fetchone()
    assert row[0] is not None, "a stated zero was written as null"
    assert float(row[0]) == 0.0


def test_an_uncosted_receipt_cannot_inflate_the_valuation(shop):
    """The number this protects.

    Receive 100 at 1,000 and 100 more with the field blank, and the
    stock used to read 200,000 against 100,000 actually spent.
    """
    con, cur = shop
    before = cur.execute(
        "select coalesce(sum(qty), 0) from public.stock_movement").fetchone()[0]
    record(cur, "RGT-1", "KHI", "grn", 100, unit_cost=1_000)
    with pytest.raises(NotAllowed):
        record(cur, "RGT-1", "KHI", "grn", 100)

    on, avg = cur.execute(
        "select soh.on_hand, c.avg_unit_cost"
        "  from public.stock_on_hand soh"
        "  join public.item_cost c on c.item_id = soh.item_id"
        " limit 1").fetchone()
    # Every unit on the shelf is backed by a price somebody stated.
    priced = cur.execute(
        "select coalesce(sum(qty), 0) from public.stock_movement"
        " where qty > 0 and unit_cost is not null").fetchone()[0]
    inbound = cur.execute(
        "select coalesce(sum(qty), 0) from public.stock_movement"
        " where qty > 0").fetchone()[0]
    assert float(priced) == float(inbound), \
        "stock went up without a cost behind it"
    assert float(on) > 0 and float(avg) > 0
    assert before is not None
