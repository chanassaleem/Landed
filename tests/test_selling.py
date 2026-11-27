"""Customers, invoices, machines and service jobs, end to end.

Against a real Postgres with the real policies, and through to the
engine modules that already compute the answers. The point of these is
the seam: engine/modules/receivables.py, machines.py and service.py
have 47 tests between them and none of them knows what a database is.
These check that the rows coming out of the schema are the shapes
those modules take.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import pytest

psycopg = pytest.importorskip("psycopg")

from engine.db.snapshot import read_invoices, read_jobs, read_machines
from engine.modules import machines as machines_mod
from engine.modules import receivables, service
from engine.schema import Thresholds

DSN = os.environ.get("LANDED_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="no LANDED_TEST_DSN")
TODAY = date.today()


@pytest.fixture
def books():
    """Two customers, four invoices, one placed machine, two jobs."""
    conn = psycopg.connect(DSN)
    cur = conn.cursor()
    cur.execute("insert into auth.users (id, email) values "
                "(gen_random_uuid(), %s) returning id",
                (f"sell-{uuid.uuid4()}@example.test",))
    owner = cur.fetchone()[0]
    cur.execute("insert into public.organisations (name, created_by) "
                "values ('Sell Co', %s) returning id", (owner,))
    org = cur.fetchone()[0]
    cur.execute("insert into public.locations (org_id, code, name) "
                "values (%s,'KHI','Karachi') returning id", (org,))
    khi = cur.fetchone()[0]

    def customer(code, name, public=False, inter=False):
        cur.execute(
            "insert into public.customers (org_id, code, name, "
            "is_public_sector, is_intercompany) values (%s,%s,%s,%s,%s) "
            "returning id", (org, code, name, public, inter))
        return cur.fetchone()[0]

    hosp = customer("CIV", "Civil Hospital", public=True)
    lab = customer("ALK", "Al Khidmat Lab")
    sister = customer("SIS", "Sister Company", inter=True)

    def invoice(no, cust, amount, paid, days_ago, terms=30):
        d = TODAY - timedelta(days=days_ago)
        cur.execute(
            "insert into public.invoices (org_id, customer_id, location_id, "
            "invoice_no, invoice_date, due_date, amount, amount_paid) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s)",
            (org, cust, khi, no, d, d + timedelta(days=terms), amount, paid))

    invoice("INV-1", hosp, 1_200_000, 0, 150)        # badly overdue
    invoice("INV-2", lab, 400_000, 100_000, 45)      # part paid, overdue
    invoice("INV-3", lab, 250_000, 250_000, 60)      # settled
    invoice("INV-4", sister, 900_000, 0, 120)        # intercompany

    cur.execute(
        "insert into public.machines (org_id, customer_id, location_id, "
        "machine_key, model, placed_date, capital_cost, contract_months, "
        "expected_monthly_tests, revenue_per_test, reagent_margin_pct) "
        "values (%s,%s,%s,'AN-1','XN-550',%s,3200000,36,900,180,0.42) "
        "returning id", (org, hosp, khi, TODAY - timedelta(days=400)))
    machine = cur.fetchone()[0]

    for no, hours, billed, cost, charged, warranty, days in (
            ("JOB-1", 6, 2, 45_000, 0, False, 1.5),
            ("JOB-2", 4, 4, 12_000, 12_000, False, 0.5)):
        cur.execute(
            "insert into public.service_jobs (org_id, machine_id, "
            "location_id, job_no, opened_date, closed_date, labour_hours, "
            "labour_hours_billed, parts_cost, parts_charged, is_warranty, "
            "downtime_days) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (org, machine, khi, no, TODAY - timedelta(days=30),
             TODAY - timedelta(days=28), hours, billed, cost, charged,
             warranty, days))
    conn.commit()

    cur.execute("set local role authenticated")
    cur.execute("select set_config('request.jwt.claims', %s, true)",
                ('{"sub": "%s", "role": "authenticated"}' % owner,))
    yield conn, cur, org

    conn.rollback()
    cur.execute("reset role")
    cur.execute("delete from public.organisations where id = %s", (org,))
    cur.execute("delete from auth.users where id = %s", (owner,))
    conn.commit()
    conn.close()


def entitle(cur, org, module):
    cur.execute("reset role")
    cur.execute("insert into public.entitlements (org_id, module, status) "
                "values (%s, %s, 'active') on conflict do nothing",
                (org, module))
    cur.execute("set local role authenticated")


# ------------------------------------------------------------ invoices

def test_invoices_read_back_as_the_engine_takes_them(books):
    conn, cur, org = books
    rows = read_invoices(cur)
    assert len(rows) == 4
    one = next(r for r in rows if r.invoice_no == "INV-1")
    assert one.customer_name == "Civil Hospital"
    assert one.is_public_sector is True
    assert one.amount == 1_200_000


def test_the_engine_computes_what_is_owed(books):
    conn, cur, org = books
    out = receivables.evaluate(read_invoices(cur), Thresholds(), TODAY)
    # The settled invoice is not owed; the intercompany one is not money.
    assert out.outstanding == pytest.approx(1_500_000)
    assert out.intercompany_excluded == pytest.approx(900_000)
    assert out.overdue > 0


def test_money_owed_by_your_own_company_is_not_money_owed(books):
    """Counted in the total it flatters the figure and nobody notices."""
    conn, cur, org = books
    out = receivables.evaluate(read_invoices(cur), Thresholds(), TODAY)
    assert out.intercompany_excluded == pytest.approx(900_000)
    assert 900_000 not in (out.outstanding, out.overdue)


def test_a_settled_invoice_is_not_outstanding(books):
    conn, cur, org = books
    out = receivables.evaluate(read_invoices(cur), Thresholds(), TODAY)
    assert out.outstanding == pytest.approx(1_200_000 + 300_000)


def test_part_payment_counts_only_the_unpaid_part(books):
    conn, cur, org = books
    rows = read_invoices(cur)
    two = next(r for r in rows if r.invoice_no == "INV-2")
    assert two.amount - two.amount_paid == 300_000


# ------------------------------------------------------------ machines

def test_a_placed_machine_reads_back_with_what_it_has_cost(books):
    conn, cur, org = books
    placements = read_machines(cur)
    assert len(placements) == 1
    m = placements[0]
    assert m.machine_key == "AN-1"
    assert m.capital_cost == 3_200_000
    # Service spend comes from the jobs, not from a column that drifts.
    assert m.service_cost_to_date == pytest.approx(57_000)
    assert m.downtime_days_ytd == pytest.approx(2.0)


def test_the_engine_judges_whether_a_placement_has_paid_back(books):
    conn, cur, org = books
    out = machines_mod.evaluate(read_machines(cur), Thresholds(), TODAY)
    assert len(out) == 1
    assert hasattr(out[0], "status")


# ------------------------------------------------------------- service

def test_jobs_keep_hours_worked_and_hours_billed_apart(books):
    """Held as one number, the gap between them -- which is the entire
    finding of engine/modules/service.py -- cannot be computed."""
    conn, cur, org = books
    jobs = read_jobs(cur)
    assert len(jobs) == 2
    assert sum(j.labour_hours for j in jobs) == 10
    assert sum(j.labour_hours_billed for j in jobs) == 6


def test_the_engine_puts_a_figure_on_the_work_given_away(books):
    conn, cur, org = books
    out = service.evaluate(read_jobs(cur), Thresholds(), TODAY)
    assert out.labour_hours == 10
    assert out.labour_hours_unbilled == 4
    assert out.parts_uncharged == pytest.approx(45_000)
    assert out.labour_value_lost > 0


# ------------------------------------------------------------- tenancy

def test_another_tenant_sees_none_of_it(books):
    """The policies, not the queries, are what keep these apart."""
    conn, cur, org = books
    cur.execute("reset role")
    cur.execute("insert into auth.users (id, email) values "
                "(gen_random_uuid(), %s) returning id",
                (f"other-{uuid.uuid4()}@example.test",))
    stranger = cur.fetchone()[0]
    conn.commit()
    cur.execute("set local role authenticated")
    cur.execute("select set_config('request.jwt.claims', %s, true)",
                ('{"sub": "%s", "role": "authenticated"}' % stranger,))

    assert read_invoices(cur) == []
    assert read_machines(cur) == []
    assert read_jobs(cur) == []

    cur.execute("reset role")
    cur.execute("delete from auth.users where id = %s", (stranger,))
    conn.commit()


# --------------------------- is "recovered so far" a measurement?
#
# read_machines had one line that decided the whole Equipment module:
#
#     actual = float(expected or 0)       # until meter readings are in
#     revenue = actual * months_run * float(per_test or 0)
#
# So the reagent revenue the recovery is computed from was the
# placement's own forecast volume times the months since placement
# times the list price per test. Not one reagent sale was consulted,
# and the function's own docstring said the opposite: "come from the
# ledger rather than from columns on the machine".
#
# The consequence is a screen that tells a distributor what share of
# PKR 32 lakh they have recovered, in a panel labelled "recovered so
# far", when the figure is a restatement of what they hoped for. A
# machine whose customer stopped buying reagents a year ago reads
# exactly like one performing to plan. Somebody deciding whether to
# chase a customer, extend a contract or place a second machine is
# reading their own forecast back.

def test_reagent_revenue_comes_from_sales_and_not_from_the_forecast(books):
    """No reagent has ever been sold against this machine, so there is
    no recovery to report -- whatever the placement hoped for."""
    conn, cur, org = books
    placements = read_machines(cur)
    m = placements[0]
    assert m.reagent_revenue_to_date == 0, (
        f"nothing was sold, yet {m.reagent_revenue_to_date:,.0f} of "
        "reagent revenue is claimed -- that is the forecast, restated")


def test_a_placement_with_no_sales_says_so_rather_than_reporting_zero(books):
    """Nothing sold and nothing recorded are different facts. The
    first is a finding; the second is an empty field."""
    conn, cur, org = books
    m = read_machines(cur)[0]
    assert m.reagent_revenue_measured is False, (
        "a placement with no reagent sales behind it must say the "
        "figure is not measured, not present a confident zero")


def test_selling_a_fitted_reagent_is_what_moves_the_recovery(books):
    """item_machines already records which reagents fit which machine,
    so the real figure is derivable. It was never consulted."""
    conn, cur, org = books
    machine = cur.execute(
        "select id from public.machines where machine_key = 'AN-1'"
    ).fetchone()[0]
    loc = cur.execute(
        "select id from public.locations limit 1").fetchone()[0]
    item = cur.execute(
        "insert into public.items (org_id, sku, description, uom,"
        " list_price) values (%s,'RGT-XN','XN-550 reagent pack','pack',"
        " 9000) returning id", (org,)).fetchone()[0]
    cur.execute(
        "insert into public.item_machines (org_id, item_id, machine_id)"
        " values (%s,%s,%s)", (org, item, machine))
    # Twelve packs sold at the list price.
    cur.execute(
        "insert into public.stock_movement (org_id, item_id, location_id,"
        " qty, reason, unit_cost, occurred_at)"
        " values (%s,%s,%s,-12,'sale',5000,now())", (org, item, loc))
    # No commit. The fixture sets the role and the JWT claims with SET
    # LOCAL, which are transaction-scoped: committing here would end
    # that transaction, drop back to the table owner, and read every
    # organisation's machines rather than this one's. It would also
    # leave a movement behind for the teardown to cascade-delete,
    # which the append-only ledger refuses outright.
    m = next(x for x in read_machines(cur) if x.machine_key == "AN-1")
    assert m.reagent_revenue_measured is True
    assert m.reagent_revenue_to_date > 0, (
        "twelve packs of a reagent that fits this machine were sold "
        "and the recovery did not move")
