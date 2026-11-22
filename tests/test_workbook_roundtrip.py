"""The workbook, imported, filling every module.

The point of the import workbook is not that rows land in tables. It
is that a person who fills it in gets a working product. So this
imports the generated example into a real database and then asks each
of the fourteen modules for its figures, which is the only way to
catch a column that imports fine and leaves a screen empty.

It also imports the same file twice. The Read me promises that is
safe, and before migration 0012 it was not: commitments, marketing
spend, dealings, contacts and the dividend had no natural key, so a
second import added every row again. For commitments that is not a
cosmetic duplicate -- the cash view sums them, so the salaries would
go out twice a month and "can I fund this order" would answer wrongly.

    source scripts/dev_postgres.sh
    python3 -m pytest tests/test_workbook_roundtrip.py
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import subprocess
import sys
import uuid
from datetime import date

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
        con.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (json.dumps({"sub": str(uid), "role": "authenticated",
                         "email": "wb@roundtrip.test"}),))
        yield con


@pytest.fixture(scope="module")
def books(tmp_path_factory) -> dict:
    """Both generated examples, as bytes."""
    out = tmp_path_factory.mktemp("wb")
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_import_workbook.py"),
         str(out)], check=True, cwd=ROOT)
    return {p.stem.replace("landed-import-", ""): p.read_bytes()
            for p in sorted(out.glob("*.xlsx"))}


@pytest.fixture(scope="module")
def imported(books):
    """One organisation with the reagent example imported into it."""
    con = psycopg.connect(DSN, autocommit=True)
    for schema in ("public", "app", "auth"):
        con.execute(f"drop schema if exists {schema} cascade")
    con.execute("create schema public")
    for path in [SHIM, *MIGRATIONS]:
        con.execute(path.read_text())

    owner = uuid.uuid4()
    con.execute("insert into auth.users (id, email) values (%s, %s)",
                (owner, "wb@roundtrip.test"))
    with as_user(con, owner) as c:
        c.execute("insert into public.organisations (name, created_by)"
                  " values ('Roundtrip Co', %s)", (owner,))

    from engine.importer import apply, plan

    with as_user(con, owner) as c:
        made = apply(c, plan(books["meridian-diagnostics"]))
    yield con, owner, made
    con.close()



def fresh_org(con, label):
    """A second organisation in the same schema.

    Not a second schema. Dropping and rebuilding public here would
    wipe the module-scoped `imported` fixture out from under every
    test that runs after this one -- which is exactly what the first
    version of these tests did, and it showed up as an unrelated
    failure three tests later. Tenancy is per organisation, so a new
    organisation is both the cleaner isolation and the more honest
    one: it is what a second customer actually is.
    """
    who = uuid.uuid4()
    con.execute("insert into auth.users (id, email) values (%s,%s)",
                (who, f"{label}-{who.hex[:8]}@roundtrip.test"))
    with as_user(con, who) as c:
        c.execute("insert into public.organisations (name, created_by)"
                  " values (%s, %s)", (f"{label} Co", who))
    return who


def counts(con, owner):
    """Row counts for every table the workbook fills."""
    tables = ("locations", "suppliers", "items", "stock_movement",
              "demand_history", "purchase_orders", "po_lines",
              "shipments", "shipment_costs", "goods_receipts",
              "grn_lines", "shipment_lines", "item_machines",
              "customers", "invoices",
              "machines", "service_jobs", "accounts", "equity_movements",
              "commitments", "cash_balances", "employees",
              "opportunities", "marketing_spend", "contacts",
              "interactions")
    with as_user(con, owner) as c:
        return {t: c.execute(
            f"select count(*) from public.{t}").fetchone()[0]
            for t in tables}


# ------------------------------------------------------ the rows land


def test_every_sheet_reaches_its_table(imported):
    """A sheet that reads and writes nothing is the failure mode worth
    catching: `shipments` was read and discarded for weeks, and the
    only symptom was a freight screen that could not be filled by
    importing."""
    con, owner, _made = imported
    got = counts(con, owner)
    empty = [t for t, n in got.items() if n == 0]
    assert not empty, f"these tables got nothing from the workbook: {empty}"


def test_the_shipment_costs_keep_their_own_basis(imported):
    """A shipping line bills by volume and customs by value. Spread
    the same way, one of them is wrong on every item."""
    con, owner, _made = imported
    with as_user(con, owner) as c:
        bases = {r[0] for r in c.execute(
            "select distinct allocate_by from public.shipment_costs"
        ).fetchall()}
        kinds = {r[0] for r in c.execute(
            "select distinct kind from public.shipment_costs").fetchall()}
    assert len(bases) > 1, f"every cost spread the same way: {bases}"
    assert "inland" in kinds and "clearing" in kinds, (
        f"cost types collapsed into one kind: {kinds}. 'Inland "
        f"transport' contains the word 'port', which a substring match "
        f"filed as clearing.")


def test_the_trial_balance_ties(imported):
    """All three checks pass on the generated example.

    A workbook whose own accounts do not balance teaches whoever opens
    it that the checks are noise. Derived rather than invented: see
    the comment in tools/make_import_workbook.py.
    """
    from web.facts import Facts
    from web.modules import run

    con, owner, _made = imported
    with as_user(con, owner) as c:
        data = run("books", Facts(c))["data"]
    assert data["statements"] is not None
    failed = [x["name"] for x in data["checks"] if not x["passed"]]
    assert not failed, f"the example's books do not tie: {failed}"


# -------------------------------------------------- the modules fill


def test_the_workbook_fills_every_module(imported):
    """Fourteen modules, one uploaded file, no empty screens.

    `missing` naming a record type means the module has nothing to
    read -- which, after importing a workbook that covers every sheet,
    means a sheet did not reach the table a module reads from.
    """
    from engine.platform import MODULES
    from web.facts import Facts
    from web.modules import run

    con, owner, _made = imported
    blank = {}
    with as_user(con, owner) as c:
        facts = Facts(c)
        for m in MODULES:
            payload = run(m.id, facts)
            if payload["missing"]:
                blank[m.id] = payload["missing"]
    assert not blank, f"modules left waiting after a full import: {blank}"


def test_each_module_comes_back_with_figures(imported):
    """Ready is not the same as useful. A module can have its record
    types and still return an empty list, which on screen is a heading
    over nothing."""
    from web.facts import Facts
    from web.modules import run

    con, owner, _made = imported
    want = ("stock", "buy", "freight", "demand", "cash", "money", "sell",
            "customers", "people", "equipment", "service",
            "intelligence")
    with as_user(con, owner) as c:
        facts = Facts(c)
        thin = {m: len(run(m, facts)["data"].get("rows") or
                       run(m, facts)["data"].get("slips") or [])
                for m in want}
    assert all(n > 0 for n in thin.values()), f"modules with no rows: {thin}"


def test_the_payroll_reads_at_the_rates_it_states(imported):
    """engine/payroll.py refuses a date it has no slab table for.

    If that fires, the figures are not wrong -- there are none -- and
    the screen says which table it would have needed.
    """
    from web.facts import Facts
    from web.modules import run

    con, owner, _made = imported
    with as_user(con, owner) as c:
        data = run("people", Facts(c))["data"]
    assert data["error"] is None, data["error"]
    assert data["slips"], "nobody on the payroll after importing Employees"
    assert data["table"]["name"], "the slab table is not named"
    assert data["table"]["source"], "the slab table cites no source"


def test_the_cash_view_opens_from_the_imported_balance(imported):
    from web.facts import Facts
    from web.modules import run

    con, owner, _made = imported
    with as_user(con, owner) as c:
        data = run("cash", Facts(c))["data"]
    assert data["needs_balance"] is False
    assert data["opening_balance"] > 0
    assert data["totals"]["payments"] > 0, (
        "no committed outflows: without them the thirteen weeks are "
        "fiction")


# --------------------------------------------------- importing twice


def test_importing_the_same_workbook_twice_changes_nothing(imported, books):
    """The Read me promises this. Before migration 0012 it was false
    for five tables."""
    from engine.importer import apply, plan

    con, owner, _made = imported
    before = counts(con, owner)
    with as_user(con, owner) as c:
        apply(c, plan(books["meridian-diagnostics"]))
    after = counts(con, owner)

    grew = {t: (before[t], after[t]) for t in before
            if after[t] != before[t]}
    assert not grew, f"a second import added rows: {grew}"


def test_the_preview_recognises_the_replay(imported, books):
    from engine.importer import inspect, plan

    con, owner, _made = imported
    with as_user(con, owner) as c:
        got = inspect(c, plan(books["meridian-diagnostics"]))
    assert got["is_replay"] is True, got["headline"]


def test_the_second_import_does_not_double_the_cash_outflow(imported):
    """The sharpest consequence of a missing key.

    The thirteen-week view sums committed payments, so a doubled
    workbook would show the salaries leaving twice a month and answer
    "can I fund this order" wrongly, in the direction that stops you
    ordering.
    """
    from web.facts import Facts
    from web.modules import run

    con, owner, _made = imported
    with as_user(con, owner) as c:
        data = run("cash", Facts(c))["data"]
        commitments = c.execute(
            "select count(*) from public.commitments").fetchone()[0]
    assert commitments == 7, f"commitments after two imports: {commitments}"
    assert data["totals"]["payments"] > 0


def test_the_books_still_tie_after_a_second_import(imported):
    """A doubled dividend fails the reserves check and reports a
    correct set of books as broken."""
    from web.facts import Facts
    from web.modules import run

    con, owner, _made = imported
    with as_user(con, owner) as c:
        data = run("books", Facts(c))["data"]
    assert data["dividends"] == 1_000_000
    failed = [x["name"] for x in data["checks"] if not x["passed"]]
    assert not failed, f"after a second import the books do not tie: {failed}"


# ----------------------------------------------- the other example too


def test_the_capital_equipment_example_imports_as_cleanly(books):
    """Two deliberately different businesses. A demo built from one
    teaches you nothing about the other, and the engine's
    classification has nothing to classify."""
    from engine.importer import apply, plan

    con = psycopg.connect(DSN, autocommit=True)
    owner = uuid.uuid4()
    con.execute("insert into auth.users (id, email) values (%s, %s)",
                (owner, f"indus-{owner.hex[:6]}@roundtrip.test"))
    with as_user(con, owner) as c:
        c.execute("insert into public.organisations (name, created_by)"
                  " values ('Indus Roundtrip', %s)", (owner,))

    p = plan(books["indus-medical-systems"])
    assert p.ok, [x.message for x in p.fatal]
    assert not p.problems, [x.message for x in p.problems[:5]]
    with as_user(con, owner) as c:
        made = apply(c, plan(books["indus-medical-systems"]))
    con.close()
    assert made["employees"] and made["pipeline"] and made["accounts"]


# ------------------------------------ the chain a customer actually has
#
# These are the tests that were missing, and their absence hid a hole
# big enough to make the product pointless.
#
# A complete workbook produced four shipments, sixteen cost lines and
# ZERO rows in public.landed_cost. The valuation tests passed because
# they wrote goods receipts straight into the database; the round-trip
# tests passed because they only ever counted shipments and costs.
# Between the two, nothing checked that an imported shipment ever
# reached an imported item -- so every figure that reads a landed unit
# cost quietly fell back to the supplier's invoice price, which is the
# one thing this product exists not to do.


def test_an_imported_shipment_reaches_the_goods_it_carried(imported):
    """Shipment, receipt, line, allocation. Each link or the rest is
    arithmetic over nothing."""
    con, owner = imported[0], imported[1]
    n = counts(con, owner)
    assert n["shipments"] >= 1
    assert n["shipment_costs"] >= 1
    assert n["goods_receipts"] >= 1, "nothing was received"
    assert n["grn_lines"] >= 1, "a receipt with no lines carries no goods"
    assert n["shipment_lines"] >= 1, \
        "the goods are not attached to the shipment, so no cost can spread"

    with as_user(con, owner) as c:
        landed = c.execute(
            "select count(*) from public.landed_cost").fetchone()[0]
    assert landed >= 1, (
        "shipments and costs imported but public.landed_cost is empty: "
        "every landed unit cost falls back to the invoice price")


def test_the_landed_cost_is_above_the_invoice_cost_and_plausibly_so(imported):
    """The product's whole claim, measured.

    Freight, duty and clearing add a fifth to a half in this trade. A
    nil uplift means the costs are not reaching the goods; a tenfold
    one means a currency is being applied twice.
    """
    con, owner = imported[0], imported[1]
    with as_user(con, owner) as c:
        rows = c.execute(
            "select i.sku, gl.unit_price * gl.fx_rate as fob,"
            "       lc.landed_unit_cost"
            "  from public.landed_cost lc"
            "  join public.grn_lines gl on gl.id = lc.grn_line_id"
            "  join public.items i on i.id = gl.item_id").fetchall()
    assert rows, "nothing to measure"
    for sku, fob, landed in rows:
        assert landed > fob, f"{sku}: landed {landed} is not above FOB {fob}"
        uplift = float(landed) / float(fob) - 1
        assert 0.02 < uplift < 1.5, (
            f"{sku}: {uplift:.1%} uplift is not a freight bill. "
            f"FOB {fob}, landed {landed}")


def test_a_foreign_invoice_is_converted_once(imported):
    """The failure this guards is specific and was real.

    The sample generator priced a euro order at the rupee figure --
    the supplier invoiced 10,120 EUR for an item worth 10,120 PKR --
    and nothing multiplied price by rate until migration 0013, at
    which point an imported workbook valued stock 305 times too high.
    A per-unit cost in the millions is the symptom.
    """
    con, owner = imported[0], imported[1]
    with as_user(con, owner) as c:
        rows = c.execute(
            "select i.sku, gl.currency, gl.unit_price, gl.fx_rate,"
            "       lc.landed_unit_cost"
            "  from public.landed_cost lc"
            "  join public.grn_lines gl on gl.id = lc.grn_line_id"
            "  join public.items i on i.id = gl.item_id").fetchall()
    for sku, currency, price, rate, landed in rows:
        if currency and currency != "PKR":
            assert float(rate) > 1, f"{sku}: {currency} at a rate of {rate}"
        assert float(landed) < 5_000_000, (
            f"{sku}: a landed unit cost of {landed} means a rate was "
            f"applied to a figure that was already converted")


def test_the_receipt_takes_the_order_currency_without_being_told(imported):
    """Migration 0013's trigger, through the import rather than
    through a hand-written row. The sheet may leave currency and rate
    blank; a receipt against a euro order is in euros by definition."""
    con, owner = imported[0], imported[1]
    with as_user(con, owner) as c:
        rows = c.execute(
            "select gl.currency, gl.fx_rate, po.currency, po.fx_rate"
            "  from public.grn_lines gl"
            "  join public.po_lines pl on pl.id = gl.po_line_id"
            "  join public.purchase_orders po on po.id = pl.po_id").fetchall()
    assert rows, "no receipt was linked to its order line"
    for gl_cur, gl_rate, po_cur, po_rate in rows:
        assert gl_cur == po_cur, f"{gl_cur} on a {po_cur} order"
        assert float(gl_rate) == float(po_rate)


def test_the_engine_values_stock_at_the_landed_cost(imported):
    """Not the Freight screen -- the stock position every other module
    reads. This is the end of the chain and the only part a customer
    sees."""
    con, owner = imported[0], imported[1]
    with as_user(con, owner) as c:
        rows = c.execute(
            "select i.sku, ic.avg_unit_cost, gl.unit_price * gl.fx_rate,"
            "       lc.landed_unit_cost"
            "  from public.landed_cost lc"
            "  join public.grn_lines gl on gl.id = lc.grn_line_id"
            "  join public.items i on i.id = gl.item_id"
            "  join public.item_cost ic on ic.item_id = gl.item_id"
            " where not exists (select 1 from public.stock_movement m"
            "                    where m.item_id = gl.item_id"
            "                      and m.source_type is distinct from"
            "                          'grn_line' and m.qty > 0)").fetchall()
    for sku, avg, fob, landed in rows:
        assert abs(float(avg) - float(landed)) < 1.0, (
            f"{sku}: the engine values this at {avg}, the landed cost is "
            f"{landed} and the invoice cost is {fob}")


def test_importing_the_same_workbook_twice_does_not_receive_twice(
        imported, books):
    """A receipt posts a stock movement, so a doubled receipt doubles
    the stock -- the most expensive kind of double."""
    from engine.importer import apply, plan

    con = imported[0]
    who = fresh_org(con, "Twice")
    with as_user(con, who) as c:
        apply(c, plan(books["meridian-diagnostics"]))
    first = counts(con, who)
    with as_user(con, who) as c:
        apply(c, plan(books["meridian-diagnostics"]))
    second = counts(con, who)

    for table in ("goods_receipts", "grn_lines", "shipment_lines",
                  "stock_movement"):
        assert first[table] == second[table], (
            f"{table}: {first[table]} became {second[table]} on a "
            f"second import of the same file")


def test_the_export_carries_the_goods_its_shipments_refer_to(imported):
    """Out and back in, with the landed cost intact.

    This is the round trip that matters. An export that carried
    shipments and costs but not the goods would produce a file that
    imports back as allocations over nothing -- exactly the state the
    import itself was in -- and the person would have no way to tell,
    because the shipment screen would look full.
    """
    from io import BytesIO

    from openpyxl import load_workbook

    from engine.exporter import to_bytes
    from engine.importer import apply, plan

    con, owner = imported[0], imported[1]
    with as_user(con, owner) as c:
        blob = to_bytes(c)

    wb = load_workbook(BytesIO(blob))
    assert "Goods received" in wb.sheetnames, \
        "the export cannot describe what it shipped"
    assert wb["Goods received"].max_row > 1, "the sheet is empty"

    # Into a fresh organisation, which is the honest test: the same
    # file, nothing else, and the valuation has to come out again.
    con2 = imported[0]
    who = fresh_org(con2, "Reimport")
    with as_user(con2, who) as c:
        apply(c, plan(blob))
    with as_user(con2, who) as c:
        rows = c.execute(
            "select i.sku, gl.unit_price * gl.fx_rate,"
            "       lc.landed_unit_cost"
            "  from public.landed_cost lc"
            "  join public.grn_lines gl on gl.id = lc.grn_line_id"
            "  join public.items i on i.id = gl.item_id").fetchall()
    assert rows, "the exported file imports back with no landed cost"
    for sku, fob, landed in rows:
        assert float(landed) > float(fob), sku


def test_a_freight_bill_in_dollars_is_converted(imported):
    """The shipping line is foreign; the clearing agent is not.

    The allocation view has multiplied by fx_rate since it was
    written, and the importer hardcoded 'PKR' with no rate -- so a
    USD freight invoice landed at a 282nd of its real weight and the
    uplift on the goods came out that much too small.
    """
    con, owner = imported[0], imported[1]
    with as_user(con, owner) as c:
        rows = c.execute(
            "select kind, amount, currency, fx_rate"
            "  from public.shipment_costs").fetchall()
    assert rows
    foreign = [r for r in rows if r[2] != "PKR"]
    assert foreign, "no cost is in a foreign currency, so this proves nothing"
    for kind, amount, currency, rate in foreign:
        assert float(rate) > 1, f"{kind} in {currency} at a rate of {rate}"
    for kind, amount, currency, rate in rows:
        if currency == "PKR":
            assert float(rate) == 1, f"{kind}: PKR at a rate of {rate}"


def test_a_shipment_with_costs_and_no_goods_is_reported(imported, books):
    """The check that would have caught the hole the Goods received
    sheet was added to fill.

    A shipment with costs and no lines is not something the database
    can refuse -- every row is valid on its own. It throws nothing,
    empties no screen and shows no obviously wrong number. It spreads
    a freight bill across zero goods, and every item quietly keeps
    its invoice price.

    The sample has two shipments still at sea, which is exactly this
    shape and is also perfectly normal, so the wording has to read as
    information rather than as a fault.
    """
    from engine.importer import apply, plan

    con = imported[0]
    who = fresh_org(con, "Stranded")
    with as_user(con, who) as c:
        p = plan(books["meridian-diagnostics"])
        apply(c, p)

    said = [x for x in p.problems if x.sheet == "Shipments"
            and "no goods" in x.message]
    assert said, "a shipment allocating to nothing was not mentioned"
    assert all(not x.fatal for x in said), \
        "goods still at sea is normal and must not fail the import"
    assert "Goods received" in said[0].message, \
        "the message does not say where to fix it"


def test_the_uplift_ignores_shipments_with_no_goods(imported):
    """Three of five shipments carrying costs and no goods made the
    Landing screen read 80.3% on top of the invoice. That is not this
    business's uplift and it is not anybody's -- it is a total divided
    by a denominator missing most of its terms.

    The excluded shipments are counted rather than hidden, because the
    reason the figure moved is the thing worth knowing: freight is
    sitting against goods nobody has booked in.
    """
    from web.facts import Facts
    from web.modules import run

    con, owner = imported[0], imported[1]
    with as_user(con, owner) as c:
        d = run("freight", Facts(c))["data"]

    t = d["totals"]
    assert t["shipments"] > t["shipments_landed"], \
        "the sample has no shipment still at sea, so this proves nothing"
    assert t["awaiting_goods"] == t["shipments"] - t["shipments_landed"]
    assert t["uplift_pct"] is not None

    # The honest figure, recomputed from the rows the screen shows.
    landed = [r for r in d["rows"] if r["goods_value"] > 0]
    want = (sum(r["landed_costs"] for r in landed)
            / sum(r["goods_value"] for r in landed))
    assert abs(t["uplift_pct"] - round(want, 4)) < 1e-6

    # And it is a freight bill, not an artefact.
    assert 0.02 < t["uplift_pct"] < 1.5, t["uplift_pct"]
    assert d["awaiting_note"], "nothing says why shipments were left out"
    assert "Goods received" in d["awaiting_note"]
