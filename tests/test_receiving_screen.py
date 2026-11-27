"""The receiving form, and what the server gives it.

The plan's test is whether a person can answer six questions without
knowing how the tables join: how much did we order, how much has
arrived, how much is still outstanding, which shipment carried these
goods, what freight was allocated, what is the final landed unit cost.

The form was driven end to end in a browser against a real imported
workbook: the order preselected with "120 of 300 has arrived; 180
still outstanding", the cost hint naming 56 EUR at 312.5, a submit
that landed a line against PO-4411 and SHP-2026-200 at 17,500 a unit
with its batch, expiry, weight and volume, and the order's outstanding
quantity falling from 180 to 140.
"""

from __future__ import annotations

import json
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
JS = (ROOT / "web" / "static" / "app.js").read_text()
CSS = (ROOT / "web" / "static" / "app.css").read_text()


# ------------------------------------- what the server offers the form


@pytest.fixture
def signed_in(monkeypatch):
    """The receiving endpoints, against a real database as a real user."""
    import time

    from engine.supabase_auth import Claims

    con = psycopg.connect(DSN, autocommit=True)
    for s in ("public", "app", "auth"):
        con.execute(f"drop schema if exists {s} cascade")
    con.execute("create schema public")
    shim = ROOT / "supabase" / "tests" / "00_supabase_shim.sql"
    for path in [shim, *sorted((ROOT / "supabase" / "migrations").glob("*.sql"))]:
        con.execute(path.read_text())

    owner = uuid.uuid4()
    con.execute("insert into auth.users (id, email) values (%s,%s)",
                (owner, "recv@screen.test"))

    monkeypatch.setattr(
        webapp, "verifier",
        lambda: type("V", (), {"verify": staticmethod(lambda t: Claims(
            user_id=str(owner), email="recv@screen.test",
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
                        lambda: RealDatabase(DSN, "recv@screen.test"))
    monkeypatch.setenv("LANDED_ENV", "dev")

    client = TestClient(webapp.app)
    assert client.post("/auth/sign-in",
                       json={"email": "recv@screen.test",
                             "password": "x"}).status_code == 200
    assert client.post("/api/organisation",
                       json={"name": "Receiving Co"}).status_code == 200

    def as_owner():
        return RealDatabase(DSN, "recv@screen.test").as_user(owner)

    with as_owner() as c:
        c.execute("insert into public.locations (org_id, code, name)"
                  " select id, 'KHI', 'Karachi' from public.organisations")
        c.execute("insert into public.items (org_id, sku, description,"
                  " lead_time_days) select id, 'RGT-1', 'Reagent', 30"
                  "  from public.organisations")
        c.execute("insert into public.suppliers (org_id, code, name)"
                  " select id, 'ACME', 'Acme' from public.organisations")
        c.execute(
            "insert into public.purchase_orders (org_id, location_id,"
            " supplier_id, po_number, currency, fx_rate, status, ordered_at,"
            " expected_at) select o.id, l.id, s.id, 'PO-1', 'EUR', 305,"
            " 'placed', current_date, current_date + 30"
            "  from public.organisations o, public.locations l,"
            "       public.suppliers s")
        c.execute(
            "insert into public.po_lines (org_id, po_id, item_id, qty,"
            " unit_price) select o.id, p.id, i.id, 300, 56"
            "  from public.organisations o, public.purchase_orders p,"
            "       public.items i")
        c.execute(
            "insert into public.shipments (org_id, reference, supplier_id,"
            " mode, status) select o.id, 'SHP-1', s.id, 'sea', 'arrived'"
            "  from public.organisations o, public.suppliers s")
        c.execute(
            "insert into public.shipments (org_id, reference, supplier_id,"
            " mode, status) select o.id, 'SHP-OLD', s.id, 'sea', 'closed'"
            "  from public.organisations o, public.suppliers s")
    yield client, as_owner
    con.close()


def test_the_form_is_told_what_is_on_order_for_this_line(signed_in):
    """Ordered, arrived, outstanding -- for this item at this branch,
    not in general."""
    client, _ = signed_in
    d = client.get("/api/receiving?sku=RGT-1&branch=KHI").json()
    assert d["ok"]
    assert len(d["orders"]) == 1
    o = d["orders"][0]
    assert (o["po_number"], o["ordered"], o["received"],
            o["outstanding"]) == ("PO-1", 300.0, 0.0, 300.0)
    # So the form can say what the price will be rather than making
    # somebody find the order and copy it.
    assert (o["unit_price"], o["currency"], o["fx_rate"]) == (56.0, "EUR",
                                                              305.0)


def test_a_closed_shipment_is_not_offered(signed_in):
    """Offering one invites somebody to hang a receipt off a shipment
    that sailed months ago."""
    client, _ = signed_in
    d = client.get("/api/receiving?sku=RGT-1&branch=KHI").json()
    refs = {s["reference"] for s in d["shipments"]}
    assert "SHP-1" in refs
    assert "SHP-OLD" not in refs


def test_receiving_part_of_an_order_leaves_the_rest_outstanding(signed_in):
    client, _ = signed_in
    r = client.post("/api/receive", json={
        "sku": "RGT-1", "branch": "KHI", "po": "PO-1", "shipment": "SHP-1",
        "qty": 120, "batch": "B-1", "expiry": "2028-03-31",
        "weight": 480, "volume": 3.0})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["linked"] is True

    d = client.get("/api/receiving?sku=RGT-1&branch=KHI").json()
    o = d["orders"][0]
    assert (o["received"], o["outstanding"]) == (120.0, 180.0)


def test_an_order_fully_received_stops_being_offered(signed_in):
    client, _ = signed_in
    client.post("/api/receive", json={
        "sku": "RGT-1", "branch": "KHI", "po": "PO-1",
        "qty": 300, "batch": "B-ALL"})
    d = client.get("/api/receiving?sku=RGT-1&branch=KHI").json()
    assert d["orders"] == []


def test_the_price_and_rate_come_from_the_order(signed_in):
    """Neither has to be typed, so neither can be mistyped."""
    client, as_owner = signed_in
    client.post("/api/receive", json={
        "sku": "RGT-1", "branch": "KHI", "po": "PO-1", "qty": 10,
        "batch": "B-1"})
    with as_owner() as c:
        price, ccy, fx, cost = c.execute(
            "select gl.unit_price, gl.currency, gl.fx_rate, m.unit_cost"
            "  from public.grn_lines gl"
            "  join public.stock_movement m on m.source_id = gl.id"
            " where gl.batch = 'B-1'").fetchone()
    assert (float(price), ccy, float(fx)) == (56.0, "EUR", 305.0)
    assert float(cost) == 56 * 305


def test_a_receipt_with_no_order_and_no_cost_is_refused(signed_in):
    """A blank would let these units take the price of whatever else
    is on the shelf."""
    client, _ = signed_in
    r = client.post("/api/receive", json={
        "sku": "RGT-1", "branch": "KHI", "qty": 5})
    assert r.status_code == 400
    assert "cost" in r.json()["error"].lower()


def test_a_correction_goes_through_the_endpoint(signed_in):
    client, as_owner = signed_in
    client.post("/api/receive", json={
        "sku": "RGT-1", "branch": "KHI", "po": "PO-1", "qty": 100,
        "batch": "B-1", "grn": "GRN-1"})
    with as_owner() as c:
        grn = c.execute("select grn_number from public.goods_receipts"
                        ).fetchone()[0]
    r = client.post("/api/receipt/correct", json={
        "grn": grn, "sku": "RGT-1", "batch": "B-1", "qty": 90,
        "why": "Recount on arrival"})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["changed"] == {"qty": [100.0, 90.0]}

    d = client.get("/api/receiving?sku=RGT-1&branch=KHI").json()
    assert d["orders"][0]["outstanding"] == 210.0


def test_a_correction_without_a_reason_is_refused(signed_in):
    client, as_owner = signed_in
    client.post("/api/receive", json={
        "sku": "RGT-1", "branch": "KHI", "po": "PO-1", "qty": 100,
        "batch": "B-1"})
    with as_owner() as c:
        grn = c.execute("select grn_number from public.goods_receipts"
                        ).fetchone()[0]
    r = client.post("/api/receipt/correct", json={
        "grn": grn, "sku": "RGT-1", "batch": "B-1", "qty": 90})
    assert r.status_code == 400
    assert "reason" in r.json()["error"]


def test_the_allocation_basis_can_be_set_through_the_endpoint(signed_in):
    client, as_owner = signed_in
    with as_owner() as c:
        c.execute("insert into public.shipment_costs (org_id, shipment_id,"
                  " kind, amount, currency, fx_rate, allocate_by)"
                  " select o.id, s.id, 'freight', 1000, 'USD', 280, 'volume'"
                  "  from public.organisations o, public.shipments s"
                  " where s.reference = 'SHP-1'")
    client.post("/api/receive", json={
        "sku": "RGT-1", "branch": "KHI", "po": "PO-1", "shipment": "SHP-1",
        "qty": 100, "batch": "B-1"})

    with as_owner() as c:
        blocked = c.execute(
            "select blocked_by from public.shipment_charge_allocation"
        ).fetchone()[0]
    assert blocked == "no volume recorded"

    r = client.post("/api/shipment/basis", json={
        "shipment": "SHP-1", "sku": "RGT-1", "batch": "B-1", "volume": 2.5,
        "why": "Bill of lading"})
    assert r.status_code == 200, r.text[:300]

    with as_owner() as c:
        blocked, allocated = c.execute(
            "select blocked_by, allocated"
            "  from public.shipment_charge_allocation").fetchone()
    assert blocked is None
    assert float(allocated) == 280_000.0


# ------------------------------------- the form itself


def test_the_form_asks_for_the_order_and_the_shipment_first():
    """The two that decide what everything below them means: the
    order supplies the price and rate, the shipment decides whether
    weight and volume do anything at all."""
    op = JS[JS.index("key: 'grn',"):]
    op = op[:op.index("send: (row, v)")]
    names = [m for m in ("po", "shipment", "qty", "unit_cost", "batch",
                         "expiry", "weight", "volume")
             if f"name: '{m}'" in op]
    assert names[:2] == ["po", "shipment"]
    assert set(names) == {"po", "shipment", "qty", "unit_cost", "batch",
                          "expiry", "weight", "volume"}


def test_weight_and_volume_are_disabled_without_a_shipment():
    """They are what freight is divided by, so with nothing to divide
    they do nothing. Disabled rather than hidden: a field that
    vanishes leaves somebody wondering where the packing list's figure
    goes."""
    op = JS[JS.index("const weighable = ()"):]
    op = op[:op.index("weighable();")]
    assert "input.disabled = !on" in op
    assert "freight is divided across" in op


def test_a_disabled_field_does_not_look_like_an_enabled_one():
    """Worse than no disabling at all: somebody types into it and
    nothing happens. Seen in a browser looking identical to the field
    above it."""
    block = CSS[CSS.index(".op-form input:disabled"):]
    block = block[:block.index("}")]
    assert "background" in block and "color" in block


def test_each_hint_sits_under_the_field_it_is_about():
    """Written as a paragraph, so without a margin of its own it
    inherits the browser's default and floats midway between its field
    and the next one -- a hint about the wrong thing."""
    block = CSS[CSS.index(".op-form .note-quiet {"):]
    block = block[:block.index("}")]
    assert "margin: -10px 0 14px" in block


def test_the_form_is_not_offered_another_suppliers_shipment(signed_in):
    """Offering every shipment in the business against every order is
    how one supplier's goods came to be received onto another's, with
    that supplier's freight allocated to them."""
    client, as_owner = signed_in
    with as_owner() as c:
        other = c.execute(
            "insert into public.suppliers (org_id, code, name)"
            " select id, 'COMEN', 'Shenzhen Comen Medical'"
            "  from public.organisations returning id").fetchone()[0]
        c.execute(
            "insert into public.shipments (org_id, reference, supplier_id,"
            " mode, status) select id, 'SHP-OTHER', %s, 'sea', 'arrived'"
            "  from public.organisations", (other,))

    d = client.get("/api/receiving?sku=RGT-1&branch=KHI").json()
    refs = {s["reference"] for s in d["shipments"]}
    assert "SHP-1" in refs, "the order's own supplier's shipment is gone"
    assert "SHP-OTHER" not in refs


def test_a_consolidation_with_no_supplier_is_still_offered(signed_in):
    client, as_owner = signed_in
    with as_owner() as c:
        c.execute(
            "insert into public.shipments (org_id, reference, mode, status)"
            " select id, 'SHP-CONSOL', 'sea', 'arrived'"
            "  from public.organisations")
    d = client.get("/api/receiving?sku=RGT-1&branch=KHI").json()
    assert "SHP-CONSOL" in {s["reference"] for s in d["shipments"]}


def test_the_form_is_told_which_orders_a_shipment_already_carries(signed_in):
    client, _ = signed_in
    client.post("/api/receive", json={
        "sku": "RGT-1", "branch": "KHI", "po": "PO-1", "shipment": "SHP-1",
        "qty": 50, "batch": "B-1"})
    d = client.get("/api/receiving?sku=RGT-1&branch=KHI").json()
    ship = [s for s in d["shipments"] if s["reference"] == "SHP-1"][0]
    assert ship["for_orders"] == ["PO-1"]


def test_receiving_onto_the_wrong_shipment_is_refused_by_the_server(
        signed_in):
    """The filter is a kindness; the refusal is the guarantee. A
    request that names the shipment directly must not get through."""
    client, as_owner = signed_in
    with as_owner() as c:
        other = c.execute(
            "insert into public.suppliers (org_id, code, name)"
            " select id, 'COMEN', 'Shenzhen Comen Medical'"
            "  from public.organisations returning id").fetchone()[0]
        c.execute(
            "insert into public.shipments (org_id, reference, supplier_id,"
            " mode, status) select id, 'SHP-OTHER', %s, 'sea', 'arrived'"
            "  from public.organisations", (other,))

    r = client.post("/api/receive", json={
        "sku": "RGT-1", "branch": "KHI", "po": "PO-1",
        "shipment": "SHP-OTHER", "qty": 10, "batch": "B-X"})
    assert r.status_code == 400
    assert "Shenzhen Comen Medical" in r.json()["error"]
