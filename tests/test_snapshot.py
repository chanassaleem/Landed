"""Reading one tenant's rows back as engine input.

Against a real Postgres with the real migrations. Reading the SQL is
not evidence: four of the columns in the first draft of this query did
not exist, and the query looked entirely reasonable.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

psycopg = pytest.importorskip("psycopg")

from engine.db.snapshot import MONTHS, read

DSN = os.environ.get("LANDED_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="no LANDED_TEST_DSN")

AS_OF = date(2026, 11, 21)


@pytest.fixture
def org(tmp_path):
    """A tenant with two branches, three items and some history."""
    conn = psycopg.connect(DSN, autocommit=False)
    cur = conn.cursor()
    cur.execute("set local role postgres")
    # created_by is a real foreign key to auth.users, so the owner has
    # to exist before the organisation can.
    # A fresh address each time: the fixture is per-test, and an email
    # is unique, so a fixed one passes once and then fails every test
    # after it in the same run.
    import uuid as _uuid

    cur.execute("insert into auth.users (id, email) values "
                "(gen_random_uuid(), %s) returning id",
                (f"snapshot-{_uuid.uuid4()}@example.test",))
    owner = cur.fetchone()[0]
    cur.execute(
        "insert into public.organisations (name, created_by) "
        "values ('Snapshot Co', %s) returning id", (owner,))
    org_id = cur.fetchone()[0]

    def one(sql, *args):
        cur.execute(sql, args)
        return cur.fetchone()[0]

    khi = one("insert into public.locations (org_id, code, name) "
              "values (%s,'KHI','Karachi') returning id", org_id)
    lhr = one("insert into public.locations (org_id, code, name) "
              "values (%s,'LHR','Lahore') returning id", org_id)

    reagent = one(
        "insert into public.items (org_id, sku, description, category, "
        "lead_time_days, is_clinical_critical) "
        "values (%s,'RGT-1','Reagent pack','Reagents',45,true) returning id",
        org_id)
    strip = one(
        "insert into public.items (org_id, sku, description, lead_time_days) "
        "values (%s,'STR-1','Urine strips',30) returning id", org_id)
    # Set up at a branch but never stocked: it must still appear, or a
    # line somebody configured and forgot is invisible.
    quiet = one(
        "insert into public.items (org_id, sku, description, lead_time_days) "
        "values (%s,'QUIET-1','Never received',21) returning id", org_id)
    cur.execute("insert into public.item_locations (org_id, item_id, "
                "location_id, lead_time_days) values (%s,%s,%s,60)",
                (org_id, quiet, khi))
    # A branch override that must beat the item default.
    cur.execute("insert into public.item_locations (org_id, item_id, "
                "location_id, lead_time_days) values (%s,%s,%s,90)",
                (org_id, reagent, lhr))

    def movement(item, loc, qty, cost, reason, when):
        cur.execute(
            "insert into public.stock_movement (org_id, item_id, location_id,"
            " qty, unit_cost, reason, occurred_at) "
            "values (%s,%s,%s,%s,%s,%s,%s)",
            (org_id, item, loc, qty, cost, reason, when))

    movement(reagent, khi, 100, 500, 'grn', date(2026, 6, 1))
    movement(reagent, khi, -30, None, 'sale', date(2026, 9, 10))
    movement(reagent, khi, -20, None, 'sale', date(2026, 10, 5))
    movement(strip, lhr, 40, 250, 'grn', date(2026, 8, 1))

    conn.commit()

    # From here the connection reads as the owner, with every policy in
    # force -- the way the application reads. As the superuser these
    # same queries pass while returning every tenant's rows, which is
    # how the join bug above survived a careful read of the SQL.
    as_user(cur, owner)
    yield conn, cur, org_id
    conn.rollback()
    # The ledger's append-only trigger refuses the DELETE that cascades
    # from organisations, which is the trigger doing its job. Worth
    # knowing: it also means an organisation cannot be deleted at all
    # while it has movements. Here the trigger is disabled for the
    # teardown only.
    cur.execute("reset role")
    cur.execute("alter table public.stock_movement disable trigger all")
    cur.execute("delete from public.organisations where id = %s", (org_id,))
    cur.execute("delete from public.organisations where created_by in "
                "(select id from auth.users where email like 'snapshot-%%' "
                "or email like 'new-%%')")
    cur.execute("delete from auth.users where email like 'snapshot-%%' "
                "or email like 'new-%%'")
    cur.execute("alter table public.stock_movement enable trigger all")
    conn.commit()
    conn.close()


def snap(cur):
    return read(cur, AS_OF)


def as_user(cur, user_id):
    """Read the way the application reads: as a signed-in person, with
    every policy in force. Reading as the superuser bypasses them, and
    a query that is wrong about tenancy looks perfectly fine."""
    cur.execute("set local role authenticated")
    cur.execute(
        "select set_config('request.jwt.claims', %s, true)",
        ('{"sub": "%s", "role": "authenticated"}' % user_id,))


def test_stock_on_hand_comes_from_the_ledger(org):
    _, cur, _ = org
    s = snap(cur)
    byk = {(i.item_key, i.location_id): i for i in s.items}
    assert byk[("RGT-1", "KHI")].on_hand == 50      # 100 - 30 - 20
    assert byk[("STR-1", "LHR")].on_hand == 40


def test_cost_is_the_average_of_what_was_received(org):
    _, cur, _ = org
    byk = {(i.item_key, i.location_id): i for i in snap(cur).items}
    assert byk[("RGT-1", "KHI")].unit_cost == pytest.approx(500)


def test_a_branch_override_beats_the_item_default(org):
    _, cur, _ = org
    byk = {(i.item_key, i.location_id): i for i in snap(cur).items}
    assert byk[("RGT-1", "LHR")].lead_time_days == 90
    assert byk[("RGT-1", "KHI")].lead_time_days == 45     # no override


def test_an_item_set_up_but_never_stocked_still_appears(org):
    """Otherwise a line somebody configured and forgot is invisible, and
    the first anyone hears of it is a stockout."""
    _, cur, _ = org
    byk = {(i.item_key, i.location_id): i for i in snap(cur).items}
    assert ("QUIET-1", "KHI") in byk
    assert byk[("QUIET-1", "KHI")].on_hand == 0


def test_combinations_the_business_never_used_are_not_invented(org):
    """An item with no stock and no setup at a branch is not a line.

    Crossing every item against every location would hand the engine a
    row for each, and each would be classified and put on screen as
    something to look at.
    """
    _, cur, _ = org
    byk = {(i.item_key, i.location_id) for i in snap(cur).items}
    assert ("QUIET-1", "LHR") not in byk
    assert ("STR-1", "KHI") not in byk


def test_a_month_with_no_demand_arrives_as_a_zero_not_a_gap(org):
    """The difference between 'we sold none' and 'we do not know'.

    The view returns only months that had movement. If the gaps are not
    filled, an item that sells in bursts reads as smooth, its
    variability reads as nil, and its safety stock comes out at zero --
    which is the failure that is invisible, because nothing errors.
    """
    _, cur, _ = org
    s = snap(cur)
    rgt = [d for d in s.demand
           if d.item_key == "RGT-1" and d.location_id == "KHI"]
    assert len(rgt) == MONTHS
    assert len({d.period for d in rgt}) == MONTHS
    sold = {d.period: d.quantity for d in rgt}
    assert sold["2026-09"] == 30
    assert sold["2026-10"] == 20
    assert sold["2026-08"] == 0          # a real zero, not a missing row


def test_sales_are_positive_demand(org):
    """The ledger stores a sale as a negative movement. Demand is a
    positive quantity -- a forecast of minus thirty is nonsense."""
    _, cur, _ = org
    rgt = [d for d in snap(cur).demand if d.item_key == "RGT-1"]
    assert all(d.quantity >= 0 for d in rgt)


def test_the_branches_are_the_ones_with_lines(org):
    _, cur, _ = org
    assert snap(cur).branches == ["KHI", "LHR"]


def test_an_organisation_with_nothing_in_it_reads_as_empty(org):
    conn, cur, _ = org
    cur.execute("reset role")
    import uuid as _uuid

    cur.execute("insert into auth.users (id, email) values "
                "(gen_random_uuid(), %s) returning id",
                (f"new-{_uuid.uuid4()}@example.test",))
    newcomer = cur.fetchone()[0]
    cur.execute(
        "insert into public.organisations (name, created_by) "
        "values ('Brand New', %s) returning id", (newcomer,))
    conn.commit()
    as_user(cur, newcomer)
    s = read(cur, AS_OF)
    assert s.is_empty is True
    assert s.items == [] and s.branches == []


def test_the_engine_accepts_what_this_produces(org):
    """The point of the whole module. It is not enough that the query
    runs -- the rows have to be the shape the engine already takes."""
    from engine import report
    from engine.schema import Thresholds

    _, cur, _ = org
    s = snap(cur)
    blocks = report.build_blocks(s.items, s.demand, Thresholds(), AS_OF)
    assert blocks
    seen = {(r.item_key, b.location_id) for b in blocks for r in b.results}
    assert ("RGT-1", "KHI") in seen


def test_two_organisations_on_one_connection_do_not_mix(org):
    """A person can belong to more than one organisation.

    The policies then legitimately return both companies' rows on the
    same connection -- that is correct, they are a member of both. What
    is not correct is pairing one company's items with the other's
    branches, which is what happens when items and locations are joined
    without org_id. The symptom is a screen full of lines that do not
    exist, at branches that are not theirs, and nothing errors.

    Today two clauses would each prevent it -- the org_id on the join
    and the WHERE that keeps only rows with a location setup or stock
    at that exact branch -- and removing either leaves this passing.
    That is deliberate. The test states the invariant rather than
    guarding one line, so it still bites when a later change makes one
    of them the only thing holding.
    """
    import uuid as _uuid

    conn, cur, first_org = org
    cur.execute("reset role")

    cur.execute("insert into auth.users (id, email) values "
                "(gen_random_uuid(), %s) returning id",
                (f"snapshot-{_uuid.uuid4()}@example.test",))
    person = cur.fetchone()[0]

    cur.execute("insert into public.organisations (name, created_by) "
                "values ('Second Co', %s) returning id", (person,))
    second_org = cur.fetchone()[0]
    cur.execute("insert into public.locations (org_id, code, name) "
                "values (%s,'ISB','Islamabad') returning id", (second_org,))
    isb = cur.fetchone()[0]
    cur.execute(
        "insert into public.items (org_id, sku, description, lead_time_days) "
        "values (%s,'OTHER-1','Someone else only',14) returning id",
        (second_org,))
    other_item = cur.fetchone()[0]
    cur.execute("insert into public.item_locations (org_id, item_id, "
                "location_id) values (%s,%s,%s)",
                (second_org, other_item, isb))

    # They do NOT join the first organisation: migration 0016 allows
    # one active membership each, because the engine groups demand
    # without an organisation in the key and two businesses under one
    # login have their sales summed.
    #
    # The invariant this test guards is unaffected. It is about the
    # SQL's join keys, not about who may see what: the role is reset
    # above, so both companies' rows are on this connection whatever
    # the policies would have done, and the question is whether the
    # snapshot pairs one company's items with the other's branches.
    conn.commit()

    # Read with the policies out of the way, so both companies' rows
    # are genuinely on this connection. That is the situation the join
    # keys have to survive: whether row-level security would have
    # filtered them is a different guarantee, tested in test_rls.py,
    # and a test that relies on it proves nothing about the SQL.
    cur.execute("reset role")
    s = read(cur, AS_OF)
    pairs = {(i.item_key, i.location_id) for i in s.items}

    assert ("OTHER-1", "ISB") in pairs
    assert ("RGT-1", "KHI") in pairs

    # But nothing is paired across them.
    assert ("OTHER-1", "KHI") not in pairs
    assert ("OTHER-1", "LHR") not in pairs
    assert ("RGT-1", "ISB") not in pairs
    assert ("STR-1", "ISB") not in pairs

    cur.execute("reset role")
    cur.execute("delete from public.organisations where id = %s", (second_org,))
    conn.commit()
