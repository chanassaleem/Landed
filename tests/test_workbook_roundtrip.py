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


def counts(con, owner):
    """Row counts for every table the workbook fills."""
    tables = ("locations", "suppliers", "items", "stock_movement",
              "demand_history", "purchase_orders", "po_lines",
              "shipments", "shipment_costs", "customers", "invoices",
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
