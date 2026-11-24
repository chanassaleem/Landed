"""The month a branch goes live, where imported history meets the ledger.

0015 settled the double count by giving the ledger any month it has
movements for. That is right for every month but the one they started
in, where the ledger holds the days since they began and the imported
history holds the whole month. 0015 took the fragment and dropped the
whole, and a forecast reading 100, 40, 105 does not see a short month
-- it sees a collapse and a recovery, which is what moves the
classification, the method and the safety stock behind it.
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


@contextlib.contextmanager
def as_user(con, uid):
    with con.transaction():
        con.execute("set local role authenticated")
        con.execute("select set_config('request.jwt.claims', %s, true)",
                    (json.dumps({"sub": str(uid), "role": "authenticated",
                                 "email": "seam@landed.test"}),))
        yield con


@pytest.fixture(scope="module")
def built():
    """A schema of its own, so the seam can be laid out by hand.

    The shared fixtures seed eighteen months of history and no ledger
    sales, which is the one shape where the seam cannot appear.
    """
    con = psycopg.connect(DSN, autocommit=True)
    for schema in ("public", "app", "auth"):
        con.execute(f"drop schema if exists {schema} cascade")
    con.execute("create schema public")
    for path in [SHIM, *MIGRATIONS]:
        con.execute(path.read_text())
    yield con
    con.close()


class Branch:
    """One organisation with one branch, written as its owner."""

    def __init__(self, con, label):
        self.con = con
        who = uuid.uuid4()
        con.execute("insert into auth.users (id, email) values (%s,%s)",
                    (who, f"{label}-{who.hex[:6]}@landed.test"))
        self.who = who
        with as_user(con, who) as c:
            c.execute("insert into public.organisations (name, created_by)"
                      " values (%s,%s)", (f"{label} Co", who))
            self.org = c.execute(
                "select o.id from public.organisations o"
                " join public.memberships m on m.org_id = o.id"
                " where m.user_id = %s", (who,)).fetchone()[0]

    def branch(self, code):
        with as_user(self.con, self.who) as c:
            return c.execute(
                "insert into public.locations (org_id, code, name)"
                " values (%s,%s,%s) returning id",
                (self.org, code, code)).fetchone()[0]

    def item(self, sku):
        with as_user(self.con, self.who) as c:
            return c.execute(
                "insert into public.items (org_id, sku, description,"
                " lead_time_days) values (%s,%s,%s,30) returning id",
                (self.org, sku, sku)).fetchone()[0]

    def history(self, loc, item, period, qty):
        with as_user(self.con, self.who) as c:
            c.execute("insert into public.demand_history (org_id,"
                      " location_id, item_id, period, quantity, source)"
                      " values (%s,%s,%s,%s,%s,'history')",
                      (self.org, loc, item, period, qty))

    def opening(self, loc, item, qty, when):
        with as_user(self.con, self.who) as c:
            c.execute("insert into public.stock_movement (org_id, item_id,"
                      " location_id, qty, unit_cost, reason, occurred_at)"
                      " values (%s,%s,%s,%s,10,'opening',%s)",
                      (self.org, item, loc, qty, when))

    def sale(self, loc, item, qty, when):
        with as_user(self.con, self.who) as c:
            c.execute("insert into public.stock_movement (org_id, item_id,"
                      " location_id, qty, reason, occurred_at)"
                      " values (%s,%s,%s,%s,'sale',%s)",
                      (self.org, item, loc, -qty, when))

    def demand(self) -> dict:
        """{(branch, sku, period): (quantity, source)}"""
        with as_user(self.con, self.who) as c:
            rows = c.execute(
                "select l.code, i.sku, d.period, d.quantity, d.source"
                "  from public.demand_monthly d"
                "  join public.locations l on l.id = d.location_id"
                "  join public.items i on i.id = d.item_id"
                " where d.org_id = %s", (self.org,)).fetchall()
        return {(r[0], r[1], r[2]): (float(r[3]), r[4]) for r in rows}


def test_the_opening_month_keeps_the_whole_month_from_history(built):
    """Live on 20 March. March sold 100 for the month; the ledger has
    the eleven days from the 20th. The month is history's."""
    b = Branch(built, "Seam")
    khi, x = b.branch("KHI"), b.item("X-1")
    b.history(khi, x, "2026-02", 100)
    b.history(khi, x, "2026-03", 100)
    b.opening(khi, x, 900, "2026-01-01")
    b.sale(khi, x, 40, "2026-03-20")
    b.sale(khi, x, 105, "2026-04-10")

    d = b.demand()
    assert d[("KHI", "X-1", "2026-02")] == (100.0, "history")
    # 40 before 0017: eleven days of a 31-day month, read by the
    # forecast as a collapse between two ordinary ones.
    assert d[("KHI", "X-1", "2026-03")] == (100.0, "history")
    assert d[("KHI", "X-1", "2026-04")] == (105.0, "ledger")


def test_a_month_after_they_went_live_is_never_filled_from_history(built):
    """Once the ledger is running its silence is a real zero. An
    imported figure for a month after go-live is stale by definition."""
    b = Branch(built, "Stale")
    khi, x = b.branch("KHI"), b.item("X-1")
    b.history(khi, x, "2026-03", 100)
    b.history(khi, x, "2026-05", 100)   # backfilled after going live
    b.opening(khi, x, 900, "2026-01-01")
    b.sale(khi, x, 40, "2026-03-20")
    b.sale(khi, x, 30, "2026-04-02")

    d = b.demand()
    assert ("KHI", "X-1", "2026-05") not in d, d
    assert d[("KHI", "X-1", "2026-04")] == (30.0, "ledger")


def test_a_slow_mover_keeps_the_quiet_months_it_really_had(built):
    """The branch opens, not each item. Taken per item, a part whose
    first sale came a month after go-live would get its own later
    cutover, and the quiet month before it -- which really did sell
    nothing -- would be overwritten by an imported figure."""
    b = Branch(built, "Slow")
    khi, fast, slow = b.branch("KHI"), b.item("FAST"), b.item("SLOW")
    b.opening(khi, fast, 900, "2026-01-01")
    b.opening(khi, slow, 200, "2026-01-01")
    b.sale(khi, fast, 40, "2026-03-20")     # the branch opens in March
    b.sale(khi, slow, 6, "2026-05-14")      # this part's first sale
    b.history(khi, slow, "2026-03", 9)
    b.history(khi, slow, "2026-04", 9)

    d = b.demand()
    # March is the opening month, so history's whole month stands.
    assert d[("KHI", "SLOW", "2026-03")] == (9.0, "history")
    # April is after it. The part sold nothing, and that is the fact,
    # not a gap to fill with last year's nine.
    assert ("KHI", "SLOW", "2026-04") not in d, d
    assert d[("KHI", "SLOW", "2026-05")] == (6.0, "ledger")


def test_a_branch_with_no_history_says_its_first_month_is_a_fragment(built):
    """Where history does not have the opening month the fragment is
    all there is. Keeping it unnamed would let a short first month pass
    for a bad one."""
    b = Branch(built, "Fresh")
    lhr, x = b.branch("LHR"), b.item("X-1")
    b.opening(lhr, x, 300, "2026-01-01")
    b.sale(lhr, x, 12, "2026-05-09")
    b.sale(lhr, x, 30, "2026-06-02")

    d = b.demand()
    assert d[("LHR", "X-1", "2026-05")] == (12.0, "ledger (part month)")
    assert d[("LHR", "X-1", "2026-06")] == (30.0, "ledger")


def test_two_branches_open_on_their_own_dates(built):
    """A branch opened later has its own seam, and the older branch's
    months are not judged by it."""
    b = Branch(built, "Pair")
    khi, lhr, x = b.branch("KHI"), b.branch("LHR"), b.item("X-1")
    b.opening(khi, x, 900, "2026-01-01")
    b.opening(lhr, x, 900, "2026-01-01")
    b.history(khi, x, "2026-03", 100)
    b.history(lhr, x, "2026-03", 50)
    b.history(lhr, x, "2026-08", 50)
    b.sale(khi, x, 40, "2026-03-20")    # KHI opens in March
    b.sale(khi, x, 90, "2026-04-04")
    b.sale(lhr, x, 20, "2026-08-11")    # LHR opens in August

    d = b.demand()
    assert d[("KHI", "X-1", "2026-03")] == (100.0, "history")
    assert d[("KHI", "X-1", "2026-04")] == (90.0, "ledger")
    # LHR's March is long before its own opening, so history holds it.
    assert d[("LHR", "X-1", "2026-03")] == (50.0, "history")
    assert d[("LHR", "X-1", "2026-08")] == (50.0, "history")


def test_history_alone_survives_until_the_first_sale(built):
    """A business that has imported and not yet sold anything through
    the app has no opening month, and keeps all of its history."""
    b = Branch(built, "Quiet")
    khi, x = b.branch("KHI"), b.item("X-1")
    for period, qty in (("2026-01", 10), ("2026-02", 20), ("2026-03", 30)):
        b.history(khi, x, period, qty)

    d = b.demand()
    assert {k[2] for k in d} == {"2026-01", "2026-02", "2026-03"}
    assert all(src == "history" for _, src in d.values())


def test_no_month_is_ever_counted_twice(built):
    """The fault 0015 was written for. Each month must come from
    exactly one branch of the union, at every shape of seam."""
    b = Branch(built, "Once")
    khi, x = b.branch("KHI"), b.item("X-1")
    b.opening(khi, x, 900, "2026-01-01")
    for period in ("2026-01", "2026-02", "2026-03", "2026-04"):
        b.history(khi, x, period, 100)
    for day in ("2026-03-20", "2026-04-10", "2026-05-01"):
        b.sale(khi, x, 40, day)

    with as_user(b.con, b.who) as c:
        dup = c.execute(
            "select count(*) from (select location_id, item_id, period"
            "   from public.demand_monthly where org_id = %s"
            "  group by 1,2,3 having count(*) > 1) z", (b.org,)).fetchone()[0]
    assert dup == 0
