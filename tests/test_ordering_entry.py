"""Raising an order, booking a shipment, and putting charges on it.

Until now these three arrived one way: a row in the import workbook.
That is fine for the first load of history and wrong for everything
after it. A buyer who has just placed an order on the phone is not
going to open Excel, add a row to "On order", and re-import the file
-- so the order does not get recorded, and the outstanding quantity
the whole Ordering screen is built on drifts away from what the
business has actually committed to.

An order is the one of the three that cannot go through the generic
record handler, because it is a header and its lines together and
both have to land or neither. A shipment and a charge are single
rows and go through the handler with a code lookup.

What the server owns, and the browser does not get a second copy of:
that a line's item exists, that the quantity is positive, that a
foreign currency carries the rate it was bought at, and that an order
number is not reused.
"""

from __future__ import annotations

import os
import pathlib
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from fastapi.testclient import TestClient

import web.app as webapp
from web import session

DSN = os.environ.get("LANDED_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="LANDED_TEST_DSN not set")

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def signed_in(monkeypatch):
    import time

    from engine.supabase_auth import Claims

    con = psycopg.connect(DSN, autocommit=True)
    for s in ("public", "app", "auth"):
        con.execute(f"drop schema if exists {s} cascade")
    con.execute("create schema public")
    shim = ROOT / "supabase" / "tests" / "00_supabase_shim.sql"
    for path in [shim,
                 *sorted((ROOT / "supabase" / "migrations").glob("*.sql"))]:
        con.execute(path.read_text())

    owner = uuid.uuid4()
    con.execute("insert into auth.users (id, email) values (%s,%s)",
                (owner, "order@entry.test"))

    monkeypatch.setattr(
        webapp, "verifier",
        lambda: type("V", (), {"verify": staticmethod(lambda t: Claims(
            user_id=str(owner), email="order@entry.test",
            expires_at=int(time.time()) + 3600))})())

    class FakeAuth(session.Auth):
        def __init__(self):
            super().__init__("https://example.supabase.co", "anon-key")

        def sign_in(self, email, password):
            return session.Tokens("access-token", "refresh-token", 3600)

    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())

    import engine.db.supabase as sup
    from tests.test_people_api import RealDatabase
    monkeypatch.setattr(sup, "database",
                        lambda: RealDatabase(DSN, "order@entry.test"))
    monkeypatch.setenv("LANDED_ENV", "dev")

    client = TestClient(webapp.app)
    assert client.post("/auth/sign-in",
                       json={"email": "order@entry.test",
                             "password": "x"}).status_code == 200
    assert client.post("/api/organisation",
                       json={"name": "Ordering Co"}).status_code == 200

    def as_owner():
        return RealDatabase(DSN, "order@entry.test").as_user(owner)

    with as_owner() as c:
        c.execute("insert into public.locations (org_id, code, name)"
                  " select id, 'KHI', 'Karachi' from public.organisations")
        c.execute("insert into public.locations (org_id, code, name)"
                  " select id, 'LHE', 'Lahore' from public.organisations")
        c.execute("insert into public.suppliers (org_id, code, name,"
                  " currency) select id, 'ROCHE', 'Roche', 'EUR'"
                  "  from public.organisations")
        for sku, desc in (("RGT-1", "Reagent"), ("SPR-2", "Probe")):
            c.execute("insert into public.items (org_id, sku, description,"
                      " lead_time_days) select id, %s, %s, 30"
                      "  from public.organisations", (sku, desc))
    yield client, as_owner
    con.close()


def rows(as_owner, sql, args=()):
    with as_owner() as c:
        return c.execute(sql, args).fetchall()


# ------------------------------------------------------- raising an order


ORDER = {
    "po_number": "PO-2026-1", "supplier": "ROCHE", "branch": "KHI",
    "currency": "EUR", "fx_rate": 305, "expected_at": "2026-12-20",
    "lines": [{"sku": "RGT-1", "qty": 300, "unit_price": 56},
              {"sku": "SPR-2", "qty": 20, "unit_price": 410}],
}


def test_an_order_and_its_lines_are_recorded_together(signed_in):
    client, as_owner = signed_in
    r = client.post("/api/order", json=ORDER)
    assert r.status_code == 200, r.text[:400]
    assert r.json()["ok"] is True

    head = rows(as_owner,
                "select po_number, currency, fx_rate, status"
                "  from public.purchase_orders")
    assert [(h[0], h[1], float(h[2]), h[3]) for h in head] == [
        ("PO-2026-1", "EUR", 305.0, "placed")]

    lines = rows(as_owner,
                 "select i.sku, pl.qty, pl.unit_price"
                 "  from public.po_lines pl"
                 "  join public.items i on i.id = pl.item_id"
                 " order by i.sku")
    assert [(l[0], float(l[1]), float(l[2])) for l in lines] == [
        ("RGT-1", 300.0, 56.0), ("SPR-2", 20.0, 410.0)]


def test_the_order_shows_up_as_outstanding_immediately(signed_in):
    """The point of recording it. Ordering reads po_outstanding, and
    an order that does not reach that view may as well not exist."""
    client, as_owner = signed_in
    assert client.post("/api/order", json=ORDER).status_code == 200
    out = rows(as_owner,
               "select po_number, outstanding from public.po_outstanding"
               " order by po_number")
    assert sorted(float(o[1]) for o in out) == [20.0, 300.0]


def said(r):
    """What an operation tells the person. Operations answer with one
    sentence under "error"; the generic record handler answers with a
    list under "problems". Both are read here."""
    d = r.json()
    return (d.get("error") or " ".join(d.get("problems") or [])).lower()


def test_an_order_with_no_lines_is_refused(signed_in):
    """A header with nothing under it is not an order, and it would
    sit in the list forever reading as fully received."""
    client, _ = signed_in
    r = client.post("/api/order", json={**ORDER, "lines": []})
    assert r.status_code == 400
    assert "line" in said(r)


def test_nothing_is_written_when_one_line_is_bad(signed_in):
    """Header and lines together or neither. A header that survived a
    rejected line is the worst of both: it reads as an order nobody
    placed, against quantities nobody agreed."""
    client, as_owner = signed_in
    r = client.post("/api/order", json={
        **ORDER,
        "lines": [{"sku": "RGT-1", "qty": 300, "unit_price": 56},
                  {"sku": "NOPE-9", "qty": 10, "unit_price": 1}],
    })
    assert r.status_code == 400
    assert "nope-9" in said(r)
    assert rows(as_owner, "select 1 from public.purchase_orders") == []
    assert rows(as_owner, "select 1 from public.po_lines") == []


def test_a_quantity_of_zero_is_refused(signed_in):
    client, _ = signed_in
    r = client.post("/api/order", json={
        **ORDER, "lines": [{"sku": "RGT-1", "qty": 0, "unit_price": 56}]})
    assert r.status_code == 400
    assert "quantity" in said(r)


def test_a_negative_quantity_is_refused(signed_in):
    client, _ = signed_in
    r = client.post("/api/order", json={
        **ORDER, "lines": [{"sku": "RGT-1", "qty": -5, "unit_price": 56}]})
    assert r.status_code == 400


def test_a_foreign_currency_without_a_rate_is_refused(signed_in):
    """Same rule the receipt obeys, and for the same reason: 56 of
    something is not a cost until it says 56 of what, at what."""
    client, _ = signed_in
    body = {k: v for k, v in ORDER.items() if k != "fx_rate"}
    r = client.post("/api/order", json=body)
    assert r.status_code == 400
    assert "rate" in said(r) and "eur" in said(r)


def test_the_base_currency_needs_no_rate(signed_in):
    client, as_owner = signed_in
    body = {k: v for k, v in ORDER.items() if k != "fx_rate"}
    r = client.post("/api/order", json={**body, "currency": "PKR"})
    assert r.status_code == 200, r.text[:300]
    assert float(rows(as_owner, "select fx_rate from"
                      " public.purchase_orders")[0][0]) == 1.0


def test_the_same_order_number_twice_is_refused_in_words(signed_in):
    client, _ = signed_in
    assert client.post("/api/order", json=ORDER).status_code == 200
    again = client.post("/api/order", json=ORDER)
    assert again.status_code == 400
    assert "po-2026-1" in said(again)


def test_a_branch_that_does_not_exist_is_named(signed_in):
    client, _ = signed_in
    r = client.post("/api/order", json={**ORDER, "branch": "PEW"})
    assert r.status_code == 400
    assert "pew" in said(r)


def test_an_order_can_be_raised_without_a_supplier(signed_in):
    """Stock bought locally for cash has no supplier record, and
    refusing it would send that purchase back to the spreadsheet."""
    client, as_owner = signed_in
    body = {k: v for k, v in ORDER.items() if k != "supplier"}
    r = client.post("/api/order", json={**body, "currency": "PKR",
                                        "fx_rate": 1})
    assert r.status_code == 200, r.text[:300]
    assert rows(as_owner, "select supplier_id from"
                " public.purchase_orders")[0][0] is None


def test_one_item_twice_on_an_order_is_refused_with_a_way_round_it(
        signed_in):
    """The schema allows one line per item per order -- 0010's
    natural key, which the import's idempotency rests on. Refusing
    it is right; refusing it with a constraint name is not."""
    client, as_owner = signed_in
    r = client.post("/api/order", json={
        **ORDER,
        "lines": [{"sku": "RGT-1", "qty": 100, "unit_price": 56},
                  {"sku": "RGT-1", "qty": 200, "unit_price": 59}],
    })
    assert r.status_code == 400
    msg = said(r)
    assert "rgt-1" in msg and "already on this order" in msg
    assert "combine" in msg, "no way round it was offered"
    assert "po_lines_natural_idx" not in msg, "that is a constraint name"
    assert rows(as_owner, "select 1 from public.purchase_orders") == []


def test_the_order_records_who_raised_it(signed_in):
    client, as_owner = signed_in
    assert client.post("/api/order", json=ORDER).status_code == 200
    assert rows(as_owner, "select created_by from"
                " public.purchase_orders")[0][0] is not None


# ---------------------------------------------------- booking a shipment


def test_a_shipment_is_booked_against_its_supplier(signed_in):
    client, as_owner = signed_in
    r = client.post("/api/record/shipment", json={
        "reference": "SHP-2026-7", "supplier_code": "ROCHE",
        "mode": "sea", "status": "in_transit",
        "etd": "2026-11-01", "eta": "2026-12-15"})
    assert r.status_code == 200, r.text[:400]
    got = rows(as_owner,
               "select s.reference, s.mode, s.status, sup.code"
               "  from public.shipments s"
               "  left join public.suppliers sup on sup.id = s.supplier_id")
    assert got == [("SHP-2026-7", "sea", "in_transit", "ROCHE")]


def test_a_consolidation_has_no_supplier_and_that_is_allowed(signed_in):
    client, as_owner = signed_in
    r = client.post("/api/record/shipment", json={
        "reference": "SHP-CONSOL", "mode": "sea", "status": "in_transit"})
    assert r.status_code == 200, r.text[:300]
    assert rows(as_owner, "select supplier_id from public.shipments"
                " where reference = 'SHP-CONSOL'")[0][0] is None


def test_the_same_shipment_reference_twice_is_refused(signed_in):
    client, _ = signed_in
    body = {"reference": "SHP-1", "mode": "sea", "status": "in_transit"}
    assert client.post("/api/record/shipment", json=body).status_code == 200
    again = client.post("/api/record/shipment", json=body)
    # 409, the convention the other records already use for a code
    # that is taken.
    assert again.status_code == 409
    assert "SHP-1" in " ".join(again.json()["problems"])


def test_a_status_the_schema_refuses_is_caught_in_words(signed_in):
    client, _ = signed_in
    r = client.post("/api/record/shipment", json={
        "reference": "SHP-X", "mode": "sea", "status": "somewhere"})
    assert r.status_code == 400
    assert "status" in " ".join(r.json()["problems"]).lower()


# ------------------------------------------------- charges on a shipment


def test_a_charge_is_put_on_a_shipment_by_its_reference(signed_in):
    client, as_owner = signed_in
    assert client.post("/api/record/shipment", json={
        "reference": "SHP-9", "mode": "sea",
        "status": "arrived"}).status_code == 200
    r = client.post("/api/record/shipment_cost", json={
        "shipment_reference": "SHP-9", "kind": "freight",
        "allocate_by": "weight", "amount": 1000,
        "currency": "USD", "fx_rate": 280})
    assert r.status_code == 200, r.text[:400]
    got = rows(as_owner,
               "select c.kind, c.allocate_by, c.amount, c.currency,"
               "       c.fx_rate, s.reference"
               "  from public.shipment_costs c"
               "  join public.shipments s on s.id = c.shipment_id")
    assert [(g[0], g[1], float(g[2]), g[3], float(g[4]), g[5])
            for g in got] == [
        ("freight", "weight", 1000.0, "USD", 280.0, "SHP-9")]


def test_a_charge_against_a_shipment_that_is_not_there_is_named(signed_in):
    client, _ = signed_in
    r = client.post("/api/record/shipment_cost", json={
        "shipment_reference": "SHP-NOPE", "kind": "freight",
        "allocate_by": "weight", "amount": 1000,
        "currency": "PKR", "fx_rate": 1})
    assert r.status_code == 400
    assert "SHP-NOPE" in " ".join(r.json()["problems"])


def test_a_basis_the_allocator_cannot_use_is_refused(signed_in):
    """'per pallet' divides by nothing the system records, so the
    charge would land nowhere and the screen would say it had."""
    client, _ = signed_in
    assert client.post("/api/record/shipment", json={
        "reference": "SHP-8", "mode": "sea",
        "status": "arrived"}).status_code == 200
    r = client.post("/api/record/shipment_cost", json={
        "shipment_reference": "SHP-8", "kind": "freight",
        "allocate_by": "pallet", "amount": 10, "currency": "PKR",
        "fx_rate": 1})
    assert r.status_code == 400
    # Named the way the form labels it, not the way the column is
    # spelled: nobody filling in a form knows what allocate_by is.
    said = " ".join(r.json()["problems"]).lower()
    assert "divided by" in said
    assert "weight" in said and "volume" in said


def test_charges_reach_the_goods_once_they_are_received(signed_in):
    """The whole point of entering them. A charge recorded on screen
    must behave exactly like one that came in on the workbook."""
    client, as_owner = signed_in
    assert client.post("/api/order", json=ORDER).status_code == 200
    assert client.post("/api/record/shipment", json={
        "reference": "SHP-5", "supplier_code": "ROCHE", "mode": "sea",
        "status": "arrived"}).status_code == 200
    assert client.post("/api/record/shipment_cost", json={
        "shipment_reference": "SHP-5", "kind": "freight",
        "allocate_by": "qty", "amount": 1000, "currency": "USD",
        "fx_rate": 280}).status_code == 200

    r = client.post("/api/receive", json={
        "sku": "RGT-1", "branch": "KHI", "po": "PO-2026-1",
        "shipment": "SHP-5", "qty": 100, "batch": "B-1", "grn": "GRN-1"})
    assert r.status_code == 200, r.text[:400]

    landed = rows(as_owner,
                  "select allocated_cost, landed_unit_cost"
                  "  from public.landed_cost")
    assert len(landed) == 1
    # The only line on the shipment, so the whole charge.
    assert float(landed[0][0]) == 280_000.0
    # 56 EUR at 305 is 17,080 a unit, plus 280,000 over 100.
    assert round(float(landed[0][1]), 2) == 17_080 + 2_800


# ------------------------------------------- the form, as it is built
#
# Read from the source, because what the browser does with a form
# spec has no endpoint of its own. The forms themselves were driven
# in Chromium against a real database: an order raised with two
# lines landed PO-SCREEN-1 with RGT-100 at 300 and SPR-200 at 20,
# both showing in po_outstanding; a shipment and a charge landed
# from the Landing screen; and the two refusals read "PO-PHONE-1 is
# already an order here" and "Line 1: there is no item NOPE-9."

FORMS = (ROOT / "web" / "static" / "records.js").read_text()
CSS = (ROOT / "web" / "static" / "app.css").read_text()


def test_the_three_records_can_be_added_from_a_screen():
    """Each on the screen whose work it belongs to: an order where
    somebody is reading what policy asks for, a shipment and its
    charges where the landed cost is worked out."""
    i = FORMS.index("const ON_MODULE = {")
    block = FORMS[i:FORMS.index("};", i)]
    assert "buy: ['order']" in block
    assert "freight: ['shipment', 'shipment_cost']" in block


def test_an_order_form_can_hold_more_than_one_line():
    """The reason an order could not go through the record builder
    before: nothing in it could collect more than one value for a
    field, and an order with no lines is not an order."""
    assert "if (type === 'lines')" in FORMS, "no repeating field type"
    i = FORMS.index("    order: {")
    spec = FORMS[i:FORMS.index("\n    shipment: {", i)]
    assert "'lines'" in spec
    for col in ("sku", "qty", "unit_price"):
        assert f"['{col}'" in spec, f"a line cannot carry its {col}"


def test_an_empty_row_is_not_sent_as_an_order_for_nothing():
    """Somebody presses "add another line" and changes their mind."""
    i = FORMS.index("if (type === 'lines') {")
    assert ".filter((row) => Object.keys(row).length)" in FORMS[i:], (
        "a row nobody typed into is still being sent")


def test_the_last_line_cannot_be_removed():
    """A form with nothing to type into reads as a form that is not
    ready, and the server would refuse it anyway."""
    i = FORMS.index("const drop = el('button', 'linkish', 'Remove');")
    assert "tbody.children.length > 1" in FORMS[i:i + 400]


def test_the_lines_table_is_given_the_whole_form_width():
    """In half a row the item code column clipped a six-character SKU
    and the remove link sat on top of the price."""
    assert ".fld-lines { grid-column: 1 / -1; }" in CSS


def test_the_lines_table_stacks_on_a_narrow_screen():
    """Three inputs across 430px leaves each too narrow to read a
    price in. Measured after the fix: 356px each, stacked."""
    # Located by the rule, then walked back to its media query.
    # There is more than one 560px block in the stylesheet, and
    # taking the first one found the wrong section entirely.
    rule = CSS.index(".line-table, .line-table tbody")
    i = CSS.rindex("@media (max-width: 560px)", 0, rule)
    depth, j = 0, CSS.index("{", i)
    k = j
    while True:
        if CSS[k] == "{":
            depth += 1
        elif CSS[k] == "}":
            depth -= 1
            if depth == 0:
                break
        k += 1
    block = CSS[i:k]
    assert "display: block" in block
    # The column widths are set with pseudo-classes, which out-specify
    # a bare `td` in here -- so each one has to be named again.
    for sel in ("td:first-child", "td:nth-child(2)", "td:nth-child(3)"):
        assert sel in block, f"{sel} keeps its desktop width on a phone"
