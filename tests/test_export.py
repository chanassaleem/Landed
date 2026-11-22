"""Getting your data back out, and proving it by putting it back in.

A business about to put its stock, its cash position and its payroll
into somebody else's software asks how it gets them out again. The
answer has to be checkable, not reassuring, so the export is the
import workbook and the test is a round trip: export, import, and the
preview reports that every row is already recorded.

That is a much stronger claim than "the file opens". A column dropped
on the way out, a date written in the wrong format, a code exported
from the wrong table -- none of those stops a file opening, and all
of them break a round trip.

    source scripts/dev_postgres.sh
    python3 -m pytest tests/test_export.py
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import subprocess
import sys
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
        con.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (json.dumps({"sub": str(uid), "role": "authenticated"}),))
        yield con


@pytest.fixture(scope="module")
def book(tmp_path_factory) -> bytes:
    out = tmp_path_factory.mktemp("wb")
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_import_workbook.py"),
         str(out)], check=True, cwd=ROOT)
    return next(out.glob("*meridian*.xlsx")).read_bytes()


@pytest.fixture(scope="module")
def filled(book):
    """An organisation with the whole example workbook imported."""
    from engine.importer import apply, plan

    con = psycopg.connect(DSN, autocommit=True)
    for schema in ("public", "app", "auth"):
        con.execute(f"drop schema if exists {schema} cascade")
    con.execute("create schema public")
    for path in [SHIM, *MIGRATIONS]:
        con.execute(path.read_text())

    owner = uuid.uuid4()
    con.execute("insert into auth.users (id, email) values (%s, %s)",
                (owner, "export@test.test"))
    with as_user(con, owner) as c:
        c.execute("insert into public.organisations (name, created_by)"
                  " values ('Export Co', %s)", (owner,))
    with as_user(con, owner) as c:
        apply(c, plan(book))
    yield con, owner
    con.close()


@pytest.fixture(scope="module")
def exported(filled) -> bytes:
    from engine import exporter

    con, owner = filled
    with as_user(con, owner) as c:
        return exporter.to_bytes(c)


# ------------------------------------------------------- what comes out


def test_the_export_is_the_import_workbook(exported):
    """Same sheets, same names. A second file format would be a second
    thing to keep in step with the importer, and the drift would show
    up as a file that exports fine and imports wrong."""
    from io import BytesIO

    from openpyxl import load_workbook

    from tools.make_import_workbook import build

    wb = load_workbook(BytesIO(exported))
    assert "Read me" in wb.sheetnames
    assert "Trial balance" in wb.sheetnames
    assert "Committed payments" in wb.sheetnames
    assert "Goods received" in wb.sheetnames

    # Against the generator rather than against a number. A count
    # tells you something changed; this tells you which sheet, and it
    # cannot drift out of date the way `== 22` did the moment a sheet
    # was added.
    assert wb.sheetnames == build("meridian", 1).sheetnames


def test_every_table_with_rows_comes_back_out(exported):
    from engine.importer import plan

    p = plan(exported).as_dict()
    assert p["ok"], p["problems"][:3]
    empty = [k for k, n in p["counts"].items() if n == 0]
    assert not empty, f"these came back empty: {empty}"


def test_the_export_reads_without_a_single_complaint(exported):
    """The importer's own warnings are the test here. A date written
    the wrong way round or a code exported from the wrong table does
    not stop the file opening; it shows up as a row the importer
    cannot place."""
    from engine.importer import plan

    p = plan(exported)
    assert not p.problems, [x.message for x in p.problems[:5]]


# ------------------------------------------------------ and goes back in


def test_importing_the_export_changes_nothing(filled, exported):
    """The round trip. Anything lost or reshaped on the way out shows
    up here as a row the importer thinks is new."""
    from engine.importer import inspect, plan

    con, owner = filled
    with as_user(con, owner) as c:
        got = inspect(c, plan(exported))
    assert got["is_replay"] is True, got["headline"]
    for table in got["tables"]:
        assert table["new"] == 0, f"{table['label']}: {table['new']} new"
        assert table["changed"] == 0, \
            f"{table['label']}: {table['changed']} changed"


def test_the_export_carries_the_landed_cost_not_the_invoice_price(filled,
                                                                  exported):
    """The one figure an export could quietly undo.

    Stock is exported at the valuation, which is the landed cost
    wherever the shipment costs are known. Exporting the supplier's
    invoice price instead would hand back a file that reimports a
    business's stock at a fifth to a half less than it cost.
    """
    from io import BytesIO

    from openpyxl import load_workbook

    con, owner = filled
    with as_user(con, owner) as c:
        want = {
            r[0]: float(r[1]) for r in c.execute(
                "select i.sku, ic.avg_unit_cost"
                "  from public.item_cost ic"
                "  join public.items i on i.id = ic.item_id").fetchall()
        }

    ws = load_workbook(BytesIO(exported))["Stock"]
    checked = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row[0] or row[0] not in want:
            continue
        assert float(row[3] or 0) == pytest.approx(want[row[0]], rel=1e-6)
        checked += 1
    assert checked > 0, "no stock rows to check"


def test_an_export_holds_only_what_that_person_can_see(filled):
    """Built through the signed-in person's own connection. As the
    service role it would be a quiet way around every policy in the
    schema -- and an export is exactly the shape an exfiltration
    takes.
    """
    from engine import exporter
    from engine.importer import plan

    con, _owner = filled
    stranger = uuid.uuid4()
    con.execute("insert into auth.users (id, email) values (%s, %s)",
                (stranger, f"{stranger.hex[:8]}@elsewhere.test"))
    with as_user(con, stranger) as c:
        theirs = exporter.to_bytes(c)

    counts = plan(theirs).as_dict()["counts"]
    assert all(n == 0 for n in counts.values()), \
        f"a stranger's export contains somebody else's data: {counts}"
