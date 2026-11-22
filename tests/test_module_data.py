"""Every module, run against a real database with real rows in it.

The unit tests in test_platform.py prove the registry is consistent.
They cannot prove a reader's SQL matches the schema, that a runner's
keys match what the engine returns, or that a module's arithmetic
survives contact with rows -- and those are exactly the mistakes that
reach a person as an empty screen or a 500.

So this seeds one organisation with every record type the platform
knows about and runs all thirteen modules over it. Seeded as the signed
-in owner, through the policies, because a fixture that writes as the
table owner proves the modules work for somebody who could not have
written the data in the first place.

    source scripts/dev_postgres.sh
    python3 -m pytest tests/test_module_data.py
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

# Inside the tax year the payroll tables actually cover. engine/
# payroll.py refuses a date it has no slab table for rather than
# computing on last year's, so a date one day earlier makes the people
# module refuse -- correctly, and for a reason that has nothing to do
# with what these tests are checking.
TODAY = date(2026, 9, 30)


@contextlib.contextmanager
def as_user(con, uid, email="owner@seed.test"):
    with con.transaction():
        con.execute("set local role authenticated")
        con.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (json.dumps({"sub": str(uid), "role": "authenticated",
                         "email": email}),))
        yield con


@pytest.fixture(scope="module")
def seeded():
    """An importer with figures in every table, as its owner would have
    entered them."""
    con = psycopg.connect(DSN, autocommit=True)
    for schema in ("public", "app", "auth"):
        con.execute(f"drop schema if exists {schema} cascade")
    con.execute("create schema public")
    for path in [SHIM, *MIGRATIONS]:
        con.execute(path.read_text())

    owner = uuid.uuid4()
    con.execute("insert into auth.users (id, email) values (%s, %s)",
                (owner, "owner@seed.test"))

    with as_user(con, owner) as c:
        org = c.execute(
            "insert into public.organisations (name, created_by, currency)"
            " values ('Seed Diagnostics', %s, 'PKR') returning id",
            (owner,)).fetchone()[0]
        khi = c.execute(
            "insert into public.locations (org_id, code, name, city)"
            " values (%s,'KHI','Karachi','Karachi') returning id",
            (org,)).fetchone()[0]
        lhe = c.execute(
            "insert into public.locations (org_id, code, name, city)"
            " values (%s,'LHE','Lahore','Lahore') returning id",
            (org,)).fetchone()[0]

    # Entitlements next, and not as the owner: migration 0001 makes
    # them writable by nobody, because what an organisation has paid
    # for is not something it tells us. Billing writes them through the
    # service role, so the seed does too -- writing them as the owner
    # would need a policy that should not exist.
    #
    # Before the rest of the seed rather than after it, because the
    # write policies on accounts, payroll and the pipeline require the
    # licence as well as the role. Seeded afterwards, those three
    # tables refuse every insert and three modules come back empty for
    # a reason that looks like missing data.
    from engine.platform import MODULES

    for m in MODULES:
        con.execute(
            "insert into public.entitlements (org_id, module)"
            " values (%s,%s) on conflict do nothing", (org, m.id))

    with as_user(con, owner) as c:
        sup = c.execute(
            "insert into public.suppliers (org_id, code, name, country,"
            " currency, lead_time_days) values"
            " (%s,'ROCHE','Roche Diagnostics','CH','EUR',75) returning id",
            (org,)).fetchone()[0]

        # Two lines with deliberately different demand shapes: a
        # reagent that moves every month and a spare that moves twice a
        # year. They classify differently, which is what makes the
        # forecast module's method selection visible.
        reagent = c.execute(
            "insert into public.items (org_id, sku, description, category,"
            " uom, supplier_id, lead_time_days, lead_time_days_std,"
            " is_clinical_critical) values (%s,'RGT-100','HbA1c reagent kit',"
            "'reagent','kit',%s,75,14,true) returning id",
            (org, sup)).fetchone()[0]
        spare = c.execute(
            "insert into public.items (org_id, sku, description, category,"
            " uom, supplier_id, lead_time_days, lead_time_days_std)"
            " values (%s,'SPR-200','Sampling probe','spare','each',%s,150,30)"
            " returning id", (org, sup)).fetchone()[0]

        for item in (reagent, spare):
            for loc in (khi, lhe):
                c.execute(
                    "insert into public.item_locations (org_id, item_id,"
                    " location_id, lead_time_days, lead_time_days_std)"
                    " values (%s,%s,%s,null,null)", (org, item, loc))

        # Opening stock, through the ledger, which is the only way
        # stock exists at all.
        for item, loc, qty, cost in (
                (reagent, khi, 420, 18500), (reagent, lhe, 60, 18500),
                (spare, khi, 14, 42000), (spare, lhe, 2, 42000)):
            c.execute(
                "insert into public.stock_movement (org_id, location_id,"
                " item_id, qty, unit_cost, reason, occurred_at)"
                " values (%s,%s,%s,%s,%s,'opening',%s)",
                (org, loc, item, qty, cost, TODAY - timedelta(days=400)))

        # Eighteen months of history. The reagent sells every month; the
        # spare sells in two bursts, so its demand is intermittent and
        # the engine has to pick a different method for it.
        for n in range(18):
            # 'YYYY-MM', which is what the column is: a month is a
            # label here, not a date, so that two branches reporting
            # the same month cannot differ by a day.
            period = f"{2025 + (n // 12)}-{(n % 12) + 1:02d}"
            for loc, base in ((khi, 90), (lhe, 25)):
                c.execute(
                    "insert into public.demand_history (org_id, location_id,"
                    " item_id, period, quantity) values (%s,%s,%s,%s,%s)",
                    (org, loc, reagent, period, base + (n % 5) * 6))
            if n in (3, 4, 11):
                c.execute(
                    "insert into public.demand_history (org_id, location_id,"
                    " item_id, period, quantity) values (%s,%s,%s,%s,%s)",
                    (org, khi, spare, period, 6))

        # An order partly received, so the open-PO arithmetic has
        # something to subtract.
        po = c.execute(
            "insert into public.purchase_orders (org_id, location_id,"
            " supplier_id, po_number, currency, fx_rate, status, ordered_at,"
            " expected_at) values (%s,%s,%s,'PO-4411','EUR',312.5,'placed',"
            "%s,%s) returning id",
            (org, khi, sup, TODAY - timedelta(days=30),
             TODAY + timedelta(days=45))).fetchone()[0]
        po_line = c.execute(
            "insert into public.po_lines (org_id, po_id, item_id, qty,"
            " unit_price) values (%s,%s,%s,300,56.0) returning id",
            (org, po, reagent)).fetchone()[0]

        grn = c.execute(
            "insert into public.goods_receipts (org_id, location_id, po_id,"
            " grn_number, received_at) values (%s,%s,%s,'GRN-900',%s)"
            " returning id",
            (org, khi, po, TODAY - timedelta(days=5))).fetchone()[0]
        grn_line = c.execute(
            "insert into public.grn_lines (org_id, grn_id, po_line_id,"
            " item_id, qty, unit_price, batch, expiry_date)"
            " values (%s,%s,%s,%s,120,56.0,'B-2026-07',%s) returning id",
            (org, grn, po_line, reagent,
             TODAY + timedelta(days=300))).fetchone()[0]

        ship = c.execute(
            "insert into public.shipments (org_id, reference, supplier_id,"
            " mode, etd, eta, status) values"
            " (%s,'SHP-77',%s,'sea',%s,%s,'arrived') returning id",
            (org, sup, TODAY - timedelta(days=60),
             TODAY - timedelta(days=6))).fetchone()[0]
        c.execute(
            "insert into public.shipment_lines (shipment_id, grn_line_id,"
            " org_id, weight_kg, volume_cbm) values (%s,%s,%s,180,1.4)",
            (ship, grn_line, org))
        # Three cost lines, each allocated the way that cost is
        # actually billed. This is the part no general ledger models.
        for kind, amount, basis in (("freight", 410_000, "volume"),
                                    ("duty", 735_000, "value"),
                                    ("clearing", 96_000, "value")):
            c.execute(
                "insert into public.shipment_costs (org_id, shipment_id,"
                " kind, amount, currency, allocate_by)"
                " values (%s,%s,%s,%s,'PKR',%s)",
                (org, ship, kind, amount, basis))

        public_hosp = c.execute(
            "insert into public.customers (org_id, code, name, city,"
            " is_public_sector, credit_limit, payment_terms_days)"
            " values (%s,'JPMC','Jinnah Hospital','Karachi',true,"
            "4000000,90) returning id", (org,)).fetchone()[0]
        private_lab = c.execute(
            "insert into public.customers (org_id, code, name, city,"
            " credit_limit, payment_terms_days)"
            " values (%s,'AGA','Aga Lab','Karachi',2000000,30) returning id",
            (org,)).fetchone()[0]

        # One current, one badly overdue, one over its limit. Each
        # exercises a different arm of the ageing.
        for no, cust, issued, due, amount, paid in (
                ("INV-1001", public_hosp, 330, 240, 5_200_000, 0),
                ("INV-1002", public_hosp, 120, 30, 1_800_000, 400_000),
                ("INV-1003", private_lab, 20, -10, 640_000, 0)):
            c.execute(
                "insert into public.invoices (org_id, customer_id,"
                " location_id, invoice_no, invoice_date, due_date, amount,"
                " amount_paid) values (%s,%s,%s,%s,%s,%s,%s,%s)",
                (org, cust, khi, no, TODAY - timedelta(days=issued),
                 TODAY - timedelta(days=due), amount, paid))

        machine = c.execute(
            "insert into public.machines (org_id, customer_id, location_id,"
            " machine_key, model, serial_no, placed_date, capital_cost,"
            " contract_months, expected_monthly_tests, revenue_per_test,"
            " reagent_margin_pct) values (%s,%s,%s,'M-01','Cobas c311',"
            "'SN-55',%s,6500000,36,900,420,0.38) returning id",
            (org, public_hosp, khi,
             TODAY - timedelta(days=500))).fetchone()[0]

        for no, opened, hours, billed, parts, charged, warranty, repeat in (
                ("JOB-01", 120, 6.0, 4.0, 38_000, 0, False, False),
                ("JOB-02", 60, 9.5, 9.5, 12_000, 12_000, False, True),
                ("JOB-03", 20, 4.0, 0.0, 5_000, 0, True, False)):
            c.execute(
                "insert into public.service_jobs (org_id, machine_id,"
                " location_id, job_no, opened_date, closed_date,"
                " labour_hours, labour_hours_billed, parts_cost,"
                " parts_charged, is_warranty, is_repeat_visit,"
                " downtime_days) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s,%s,%s)",
                (org, machine, khi, no, TODAY - timedelta(days=opened),
                 TODAY - timedelta(days=opened - 3), hours, billed, parts,
                 charged, warranty, repeat, 4))

        c.execute(
            "insert into public.cash_balances (org_id, as_at, balance)"
            " values (%s,%s,3250000)", (org, TODAY - timedelta(days=1)))
        for label, kind, due, amount, recurring, every in (
                ("LC Roche PO-4411", "lc_settlement", 10, 5_250_000,
                 False, 0),
                ("Salaries", "payroll", 2, 1_850_000, True, 30),
                ("Warehouse rent", "rent", 8, 320_000, True, 30)):
            c.execute(
                "insert into public.commitments (org_id, location_id, label,"
                " kind, due_date, amount, is_recurring, recurs_every_days)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s)",
                (org, khi, label, kind, TODAY + timedelta(days=due), amount,
                 recurring, every))

        # A trial balance that actually balances, and whose reserves
        # and cash flow tie. Worked backwards from the three checks
        # rather than invented: see the two closing figures below.
        for code, name, line, opening, closing in (
                ("4000", "Sales", "revenue", 0, 48_000_000),
                ("5000", "Cost of sales", "cogs", 0, 31_200_000),
                ("6000", "Salaries", "payroll", 0, 6_400_000),
                ("6100", "Other overheads", "opex", 0, 3_100_000),
                ("6200", "Depreciation", "depreciation", 0, 1_250_000),
                ("7000", "Bank interest", "finance_cost", 0, 2_050_000),
                ("8000", "Tax", "tax", 0, 1_200_000),
                ("1000", "Bank", "cash", 2_100_000, 3_250_000),
                ("1100", "Trade debtors", "receivables",
                 5_900_000, 7_240_000),
                ("1200", "Stock", "inventory", 9_400_000, 10_830_000),
                ("1500", "Equipment", "fixed_assets",
                 14_000_000, 20_500_000),
                ("1510", "Depreciation to date", "accumulated_depreciation",
                 3_000_000, 4_250_000),
                # Closing creditors and closing reserves are not
                # round numbers because they are not free: they are
                # what makes all three checks tie. Reserves must move
                # by profit after tax less the dividend
                # (8.2 + 2.8 - 1.0 = 10.0), and creditors are then
                # whatever balances the sheet against assets of 37.57.
                # A seed whose books do not balance cannot prove the
                # checks are right -- only that they fire.
                ("2000", "Trade creditors", "payables",
                 6_300_000, 12_270_000),
                ("2100", "Bank facility", "short_term_debt",
                 8_000_000, 9_000_000),
                ("2200", "Tax payable", "tax_payable", 900_000, 1_300_000),
                ("3000", "Share capital", "equity", 5_000_000, 5_000_000),
                ("3100", "Retained earnings", "retained_earnings",
                 8_200_000, 10_000_000)):
            c.execute(
                "insert into public.accounts (org_id, code, name, line,"
                " period_from, period_to, opening, closing)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s)",
                (org, code, name, line, date(2026, 1, 1), TODAY,
                 opening, closing))
        c.execute(
            "insert into public.equity_movements (org_id, kind, moved_on,"
            " amount) values (%s,'dividend',%s,1000000)",
            (org, date(2026, 3, 31)))

        for no, name, dept, basic, allow, med in (
                ("E-001", "Imran Shah", "sales", 185_000, 40_000, 12_000),
                ("E-002", "Sana Raza", "service", 145_000, 30_000, 10_000),
                ("E-003", "Bilal Khan", "warehouse", 62_000, 8_000, 4_000)):
            c.execute(
                "insert into public.employees (org_id, location_id,"
                " employee_no, name, department, basic_monthly,"
                " allowances_monthly, medical_monthly, employer_pf_rate,"
                " employee_pf_rate, joined) values"
                " (%s,%s,%s,%s,%s,%s,%s,%s,0.05,0.05,%s)",
                (org, khi, no, name, dept, basic, allow, med,
                 date(2023, 4, 1)))

        for ref, cust, stage, value, close, item in (
                ("TND-500", public_hosp, "tender_submitted", 9_800_000,
                 45, reagent),
                ("TND-501", private_lab, "quoted", 1_250_000, 20, spare),
                ("TND-490", public_hosp, "won", 4_100_000, -60, reagent),
                ("TND-480", private_lab, "lost", 800_000, -90, spare)):
            c.execute(
                "insert into public.opportunities (org_id, customer_id,"
                " location_id, reference, stage, value, expected_close,"
                " item_id, quantity, source, owner_name, opened)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,120,'tender','Imran',%s)",
                (org, cust, khi, ref, stage, value,
                 TODAY + timedelta(days=close), item,
                 TODAY - timedelta(days=200)))
        for label, cat, days, amount in (
                ("Pathology conference", "conference", 70, 450_000),
                ("Demo unit at JPMC", "demo_unit", 120, 900_000),
                ("Sample kits", "samples", 40, 180_000)):
            c.execute(
                "insert into public.marketing_spend (org_id, label,"
                " category, spent_on, amount) values (%s,%s,%s,%s,%s)",
                (org, label, cat, TODAY - timedelta(days=days), amount))

        contact = c.execute(
            "insert into public.contacts (org_id, customer_id, name,"
            " role_title, phone, is_primary) values"
            " (%s,%s,'Dr Farooq','Head of Pathology','+92...',true)"
            " returning id", (org, public_hosp)).fetchone()[0]
        c.execute(
            "insert into public.interactions (org_id, customer_id,"
            " contact_id, happened_on, channel, summary, created_by)"
            " values (%s,%s,%s,%s,'visit','Discussed the tender',%s)",
            (org, public_hosp, contact, TODAY - timedelta(days=9), owner))

    yield con, owner, org
    con.close()


@pytest.fixture
def facts(seeded):
    """A Facts for the signed-in owner, inside one transaction.

    Read through the policies, as a request would, so a module that
    only works for the table owner fails here.
    """
    con, owner, _ = seeded
    from web.facts import Facts

    with as_user(con, owner) as c:
        yield Facts(c, as_of=TODAY)


# ------------------------------------------------------------- presence


def test_every_record_type_the_platform_knows_about_is_present(facts):
    """The seed is complete, so nothing below is quiet for want of rows.

    If this fails, the tests after it are not testing what they say
    they are -- a module with no records returns `missing` and no data,
    which would read as passing.
    """
    from engine.platform import RECORDS

    assert set(facts.present) == set(RECORDS), \
        f"seed is missing: {set(RECORDS) - set(facts.present)}"


def test_every_module_reports_itself_ready(facts):
    from engine.platform import catalogue

    states = {m["id"]: m["state"] for m in catalogue(present=facts.present)}
    assert set(states.values()) == {"ready"}, states


# -------------------------------------------------------- every module


def test_every_module_runs_and_returns_data(facts):
    """Thirteen modules, one seeded organisation, no exceptions.

    The loop rather than thirteen tests because the failure that
    matters is "a module broke", and naming which one is what the
    assertion message is for.
    """
    from web.modules import run
    from engine.platform import MODULES

    for m in MODULES:
        payload = run(m.id, facts)
        assert payload["missing"] == [], f"{m.id}: {payload['missing']}"
        assert payload["data"] is not None, f"{m.id} returned no data"


def test_no_module_is_refused_to_the_owner(facts):
    """A refusal here means a policy is stricter than the role gate.

    The owner can open every module, so anything the readers could not
    read is a policy that disagrees with engine/platform.py -- and the
    screen would show an empty module with no explanation.
    """
    from web.modules import run
    from engine.platform import MODULES

    for m in MODULES:
        run(m.id, facts)
    assert facts.refused == set(), f"refused to an owner: {facts.refused}"


def test_json_survives_a_round_trip(facts):
    """Every payload has to go down a wire.

    A date, an enum or a tuple-keyed dict left in a result raises at
    serialising time, which in FastAPI is after the handler returned --
    so it surfaces as a 500 with no traceback pointing at the module.
    """
    from web.modules import run
    from engine.platform import MODULES

    for m in MODULES:
        json.dumps(run(m.id, facts))


# ------------------------------------------- what each module should say


def test_stock_sees_both_branches_and_prices_the_excess(facts):
    from web.modules import run

    data = run("stock", facts)["data"]
    assert data["totals"]["branches"] == 2
    assert data["totals"]["lines"] >= 4
    assert data["totals"]["stock_value"] > 0


def test_stock_explains_a_surplus_it_cannot_move(facts):
    """Lahore is short of the reagent and Karachi is flagged long on it,
    and yet nothing can move. The product has to say why.

    EXCESS is a flat six months of on-hand cover; the level a donor
    keeps is its order-up-to, which is lead-time relative. At the
    reagent's 75-day lead time order-up-to is about seven and a half
    months of demand, so it sits above the line that flagged the line
    as standing still. Both halves are defensible and the pair
    contradicts itself.

    Resolving it changes what EXCESS means on every stock screen, so it
    is a decision for the business. What is not acceptable is the
    symptom this test pins against: an empty transfer list, no error,
    and the most valuable output in the product appearing to do
    nothing at all.
    """
    data = run_module(facts, "stock")
    assert data["transfers"] == [], \
        "the seed was meant to reproduce the thresholds crossing"
    assert data["blocked_donors"], \
        "a surplus that cannot move was not reported at all"
    blocked = data["blocked_donors"][0]
    assert blocked["order_up_to"] > blocked["on_hand"]
    assert blocked["months_cover"] > blocked["excess_limit_months"]
    assert "Settings" in data["blocked_note"]


def test_stock_does_suggest_moving_when_the_limits_do_not_cross(facts):
    """The other half, and proof the module is not simply inert.

    Lowering the cover target is the resolution that frees stock: it
    lowers the level the sending branch has to keep, so the surplus
    above it becomes movable. Raising the standing-still limit is the
    other resolution and does something different -- it stops the line
    being flagged at all, which is the right answer when the stock is
    held deliberately for a long lead time, and it produces no
    transfer. Pinned here because a note that offered only the second
    would send somebody to the one knob that cannot give them what
    they are looking for.
    """
    import dataclasses

    from web.facts import Facts

    relaxed = dataclasses.replace(facts.thresholds,
                                  target_cover_months=1.5)
    loose = Facts(facts.conn, thresholds=relaxed, as_of=facts.as_of)
    assert loose.transfers, \
        "no transfer even once the two limits no longer cross"
    assert sum(t["import_avoided"] for t in loose.transfers) > 0


def test_demand_picks_a_different_method_per_pattern(facts):
    """The reagent moves monthly, the spare twice a year.

    One method for both would be the mistake module 1 exists to
    prevent: ordinary smoothing on intermittent demand drifts toward
    the mean of a series that is mostly zeros.
    """
    data = run_module(facts, "demand")
    methods = {r["method"] for r in data["rows"]}
    assert len(methods) > 1, f"one method for every pattern: {methods}"


def test_cash_opens_from_the_balance_that_was_entered(facts):
    data = run_module(facts, "cash")
    assert data["opening_balance"] == 3_250_000
    assert data["needs_balance"] is False
    assert len(data["rows"]) == 13


def test_cash_counts_the_committed_outflows(facts):
    """Without commitments the horizon is fiction: the letter of credit
    settles and the salaries go out whatever the stock position says."""
    data = run_module(facts, "cash")
    assert data["totals"]["payments"] > 0


def test_buy_is_ranked_and_says_what_it_cannot_fund(facts):
    data = run_module(facts, "buy")
    assert data["cash_known"] is True
    assert data["caveat"] is None
    assert data["totals"]["requested"] >= 0


def test_money_excludes_nothing_it_should_not_and_ages_the_book(facts):
    data = run_module(facts, "money")
    assert data["totals"]["outstanding"] == pytest.approx(
        5_200_000 + 1_400_000 + 640_000, rel=1e-6)
    assert data["totals"]["overdue"] > 0
    assert data["totals"]["dso_days"] is not None


def test_books_balance_and_the_checks_are_reported(facts):
    """The checks are the output, not a footnote.

    A cash flow that does not tie means a working-capital movement has
    been missed, so a statement presented without them is a tidy
    picture of an unknown.
    """
    data = run_module(facts, "books")
    assert data["statements"] is not None
    assert data["checks"], "the statements came back with no checks"
    assert data["dividends"] == 1_000_000
    assert data["period"]["to"] == TODAY.isoformat()

    # All three pass, which is the only way to know the checks are
    # right rather than merely loud. The seed balances, its reserves
    # move by profit less the dividend, and the indirect cash flow
    # lands on the movement in cash.
    failed = [c["name"] for c in data["checks"] if not c["passed"]]
    assert not failed, f"the seeded books do not tie: {failed}"


def test_freight_prices_the_uplift_on_the_invoice(facts):
    """Landed cost adds a fifth to a half on top of FOB in this trade.

    A business pricing off the supplier invoice is wrong by that much
    on every line, which is the reason this module exists.
    """
    data = run_module(facts, "freight")
    assert data["totals"]["landed_costs"] == 410_000 + 735_000 + 96_000
    assert data["totals"]["uplift_pct"] > 0
    bases = {b["allocate_by"] for b in data["breakdown"]}
    assert bases == {"value", "volume"}, \
        "every cost allocated the same way, which is wrong for one of them"


def test_equipment_judges_payback_against_the_contract(facts):
    data = run_module(facts, "equipment")
    assert data["totals"]["placements"] == 1
    assert data["totals"]["capital_deployed"] == 6_500_000
    assert data["rows"][0]["payback_months"] is not None


def test_service_counts_the_hours_nobody_billed(facts):
    """The gap between hours worked and hours billed is absent from
    every ERP in this market, because none of them is told what a job
    should have cost."""
    data = run_module(facts, "service")
    assert data["totals"]["value_lost"] > 0
    assert data["totals"]["repeat_visit_rate"] > 0


def test_service_does_not_count_warranty_work_as_wastage(facts):
    """Work under warranty was priced in at the point of supply.

    Counting it as wastage double counts it and points the fix at the
    wrong place. JOB-03 is warranty with four unbilled hours, so a
    module that counted it would be visibly higher.
    """
    data = run_module(facts, "service")
    rate = facts.thresholds.labour_rate_per_hour
    # JOB-01 alone: two unbilled hours and 38,000 of parts not charged.
    assert data["totals"]["value_lost"] == pytest.approx(
        2.0 * rate + 38_000, rel=1e-6)


def test_people_pays_at_the_rates_in_settings(facts):
    data = run_module(facts, "people")
    assert data["error"] is None, data["error"]
    assert len(data["slips"]) == 3


def test_intelligence_checks_the_pipeline_against_the_shelf(facts):
    """A tender closing inside the lead time with nothing on the shelf
    is a disappointment with a date on it, and only a product holding
    the stock position can say so."""
    data = run_module(facts, "intelligence")
    assert data["rows"], "the pipeline came back empty"
    assert data["cash_known"] is True


def test_customers_warns_before_the_next_order_not_after(facts):
    data = run_module(facts, "customers")
    assert data["totals"]["customers"] == 2
    hosp = [c for c in data["rows"] if c["code"] == "JPMC"][0]
    assert hosp["outstanding"] == 5_200_000 + 1_400_000
    assert hosp["over_limit"] is True, \
        "a customer 2.6M over a 4M limit was not flagged"
    assert hosp["last_contact"] is not None


def test_sell_does_not_invent_a_margin(facts):
    """Margin needs the cost of what went out, and invoice lines do not
    exist yet. A figure apportioned across whatever sold that month
    would be plausible, baseless, and the number people would price
    from."""
    data = run_module(facts, "sell")
    assert data["totals"]["invoices"] == 3
    assert "margin" not in str(data["rows"][0].keys())
    assert data["note"]


def run_module(facts, module_id):
    from web.modules import run

    payload = run(module_id, facts)
    assert payload["data"] is not None, \
        f"{module_id} returned nothing: {payload}"
    return payload["data"]


# ------------------------------------------- the checks nobody could see


def test_clean_figures_report_nothing_known_wrong(facts):
    """And say exactly that, rather than "no problems".

    These checks find what they look for. Silence is not proof the
    figures are right, and a report that implies otherwise is worse
    than no report.
    """
    data = run_module(facts, "quality")
    assert data["totals"]["total"] == 0
    assert data["totals"]["nothing_known_wrong"] is True
    assert "not the same as right" in data["verdict"]


def test_a_fault_in_the_figures_is_found_and_explained(seeded):
    """A lead time of zero, which makes a reorder point meaningless.

    Written and undone inside the test so the clean seed the other
    tests rely on survives. The point is not that this one check
    works -- it is that engine/sanity.py reaches a screen at all.
    Until now its 657 lines of checks ran on every import and their
    findings went into the command-line report and stopped there.
    """
    con, owner, _org = seeded
    from web.facts import Facts
    from web.modules import run

    with as_user(con, owner) as c:
        c.execute("update public.items set lead_time_days = 0"
                  " where sku = 'RGT-100'")
    try:
        with as_user(con, owner) as c:
            data = run("quality", Facts(c, as_of=TODAY))["data"]
    finally:
        with as_user(con, owner) as c:
            c.execute("update public.items set lead_time_days = 75"
                      " where sku = 'RGT-100'")

    codes = {r["code"] for r in data["rows"]}
    assert "lead_time_zero" in codes, f"not caught: {codes}"

    found = [r for r in data["rows"] if r["code"] == "lead_time_zero"][0]
    # A list of faults with no consequence and no remedy is homework.
    assert found["effect"], "the finding does not say what it does"
    assert found["fix"], "the finding does not say how to fix it"
    assert found["severity"] in ("blocks", "warns", "notes")
    assert data["totals"]["total"] >= 1


def test_the_clean_seed_is_restored(facts):
    """Guards the guard above: a fault left behind would quietly make
    every later run of this file report a problem that is not real."""
    assert run_module(facts, "quality")["totals"]["total"] == 0


# ----------------------------------------------- narrowing to one branch
#
# A global branch selector used to narrow one screen client-side and
# silently not narrow the other thirteen. Scope belongs before the
# modules run: narrow the facts, then compute, so every figure is
# computed on exactly the rows the screen is showing.


def scoped(seeded, branch):
    from web.facts import Facts

    con, owner, _org = seeded
    with as_user(con, owner) as c:
        yield Facts(c, as_of=TODAY, branch=branch)


def test_narrowing_moves_the_rows_and_the_totals_together(seeded):
    """The whole reason this is not a row filter in the page.

    Filtered rows under an unfiltered total means the table and the
    number above it disagree, and the number is the one people quote.
    """
    from web.modules import run

    whole = next(scoped(seeded, None))
    khi = next(scoped(seeded, "KHI"))

    a = run("stock", whole)["data"]
    b = run("stock", khi)["data"]
    assert b["totals"]["branches"] == 1
    assert a["totals"]["branches"] == 2
    assert len(b["rows"]) < len(a["rows"])
    assert b["totals"]["stock_value"] < a["totals"]["stock_value"]
    assert b["totals"]["stock_value"] == pytest.approx(
        sum(r["stock_value"] for r in b["rows"]), rel=1e-6)


def test_a_narrowed_module_recomputes_rather_than_filters(seeded):
    """Safety stock comes from a location's own demand history, so it
    cannot be recovered by adding up rows that already went through
    the engine. Narrowing has to happen before the module runs."""
    from web.modules import run

    khi = next(scoped(seeded, "KHI"))
    rows = run("stock", khi)["data"]["rows"]
    assert rows and all(r["location_id"] == "KHI" for r in rows)
    assert all(r["reorder_point"] > 0 for r in rows), \
        "the policy was not recomputed for this branch"


def test_the_cash_view_stays_on_the_group(seeded):
    """One bank balance serves the business. Thirteen weeks for one
    branch against the group's opening balance would invent a balance
    that branch does not have."""
    from web.modules import run

    whole = run("cash", next(scoped(seeded, None)))["data"]
    khi = run("cash", next(scoped(seeded, "KHI")))["data"]
    assert khi["opening_balance"] == whole["opening_balance"]
    assert khi["totals"]["payments"] == whole["totals"]["payments"]


def test_transfers_are_computed_across_the_group(seeded):
    """A transfer has a sender and a receiver. Narrow the facts to one
    branch and the module cannot see the other side of the move it
    exists to suggest -- it would return nothing and look like there
    was nothing worth moving."""
    import dataclasses

    from web.facts import Facts

    con, owner, _org = seeded
    loose = dataclasses.replace(Facts(con).thresholds,
                                target_cover_months=1.5)
    with as_user(con, owner) as c:
        whole = Facts(c, thresholds=loose, as_of=TODAY)
        assert whole.transfers, "the seed should produce a transfer"
    with as_user(con, owner) as c:
        khi = Facts(c, thresholds=loose, as_of=TODAY, branch="KHI")
        moves = khi.transfers
    assert moves, "narrowing to one branch lost the transfer entirely"
    assert all("KHI" in (t["from_location"], t["to_location"])
               for t in moves)


def test_a_branch_that_holds_nothing_narrows_to_nothing(seeded):
    """Not to everything. A filter that silently matches nothing and
    falls back to the whole group is worse than an empty screen."""
    from web.modules import run

    nowhere = next(scoped(seeded, "ZZZ"))
    data = run("stock", nowhere)["data"]
    assert data["rows"] == []
    assert data["totals"]["stock_value"] == 0
