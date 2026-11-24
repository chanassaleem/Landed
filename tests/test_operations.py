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


# ------------------------------------- two at once, and the same one twice
#
# The availability check reads the on-hand figure and then inserts.
# Two sales started together both read the same figure, both find it
# sufficient and both insert -- each transaction correct when it
# looked, the ledger wrong when they finish. Proven on a clean
# database: 100 on the shelf, two sales of 80, both committed, ledger
# at -60. Nothing errors, because nothing was wrong at the time.


def test_two_sales_at_once_cannot_sell_the_same_stock(shop):
    import threading
    import time

    import psycopg

    con, cur = shop
    dsn = con.info.dsn
    # No commit here. The fixture already committed the shop's rows,
    # and its role and claims are transaction-local -- committing
    # would discard them, and the side connections would be told this
    # account has no organisation.
    claims = cur.execute(
        "select current_setting('request.jwt.claims', true)").fetchone()[0]
    assert claims, "the fixture's claims were lost"
    have = float(cur.execute(
        "select coalesce(sum(qty), 0) from public.stock_movement"
    ).fetchone()[0])
    assert have >= 100, "the fixture has too little stock to race over"
    start = threading.Barrier(2)
    done = []

    def sell():
        side = psycopg.connect(dsn)
        try:
            c = side.cursor()
            c.execute("set role authenticated")
            c.execute("select set_config('request.jwt.claims', %s, false)",
                      (claims,))
            start.wait(timeout=10)
            record(c, "RGT-1", "KHI", "sale", have - 10)
            time.sleep(0.05)
            side.commit()
            done.append("committed")
        except Exception as exc:
            side.rollback()
            done.append(f"refused: {type(exc).__name__}: {exc}"[:90])
        finally:
            side.close()

    threads = [threading.Thread(target=sell) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    kinds = sorted(d.split(":")[0] for d in done)
    assert kinds == ["committed", "refused"], done
    left = float(cur.execute(
        "select coalesce(sum(qty), 0) from public.stock_movement"
    ).fetchone()[0])
    assert left >= 0, f"the shelf went to {left}"


def test_the_same_request_twice_records_one_movement(shop):
    """A reply that never arrived gets retried with the same key. The
    ledger has had a unique index on (org_id, client_ref) since it was
    written; this generated a fresh uuid every call, so the guarantee
    was there and nothing used it."""
    _, cur = shop
    key = f"attempt-{uuid.uuid4()}"

    first = record(cur, "RGT-1", "KHI", "sale", 5, client_ref=key)
    again = record(cur, "RGT-1", "KHI", "sale", 5, client_ref=key)

    assert first["recorded"] is True
    assert again["recorded"] is False, "the retry posted a second sale"
    n = cur.execute(
        "select count(*) from public.stock_movement where client_ref = %s",
        (key,)).fetchone()[0]
    assert n == 1


def test_two_different_attempts_both_record(shop):
    """Idempotency must not turn two deliberate sales into one."""
    _, cur = shop
    before = float(cur.execute(
        "select coalesce(sum(qty), 0) from public.stock_movement"
    ).fetchone()[0])
    record(cur, "RGT-1", "KHI", "sale", 3, client_ref=f"a-{uuid.uuid4()}")
    record(cur, "RGT-1", "KHI", "sale", 3, client_ref=f"b-{uuid.uuid4()}")
    after = float(cur.execute(
        "select coalesce(sum(qty), 0) from public.stock_movement"
    ).fetchone()[0])
    assert before - after == 6
