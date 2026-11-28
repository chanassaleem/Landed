"""Receiving a whole shipment, rather than one line eight times.

A container arrives with eight lines on it. Until now the only way
to record that was to open each stock row's drawer in turn, press
"Receive stock", and re-select the purchase order and the shipment
every time -- eight times, with the same two answers typed into
every one of them. Nobody does that eight times; they do it twice
and record the rest next week, or not at all, and the stock figure
drifts from the shelf.

What the page needs from the server is the other half of the same
problem: the person should not have to remember what was on the
order. Given a shipment, it says what that shipment could be
carrying and how much of each is still outstanding, so the form
arrives already filled in and the only thing typed is what actually
turned up.

All of the lines land or none of them do. Half a container recorded
is worse than none: the stock is wrong in a way that looks right,
and nothing says which half is missing.
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
def ready(monkeypatch):
    """An order with three lines, and a shipment to carry them."""
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
                (owner, "many@recv.test"))

    monkeypatch.setattr(
        webapp, "verifier",
        lambda: type("V", (), {"verify": staticmethod(lambda t: Claims(
            user_id=str(owner), email="many@recv.test",
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
                        lambda: RealDatabase(DSN, "many@recv.test"))
    monkeypatch.setenv("LANDED_ENV", "dev")

    client = TestClient(webapp.app)
    assert client.post("/auth/sign-in",
                       json={"email": "many@recv.test",
                             "password": "x"}).status_code == 200
    assert client.post("/api/organisation",
                       json={"name": "Container Co"}).status_code == 200

    def as_owner():
        return RealDatabase(DSN, "many@recv.test").as_user(owner)

    with as_owner() as c:
        c.execute("insert into public.locations (org_id, code, name)"
                  " select id, 'KHI', 'Karachi' from public.organisations")
        c.execute("insert into public.suppliers (org_id, code, name)"
                  " select id, 'ROCHE', 'Roche' from public.organisations")
        for sku, desc in (("RGT-1", "Reagent"), ("RGT-2", "Buffer"),
                          ("SPR-3", "Probe")):
            c.execute("insert into public.items (org_id, sku, description,"
                      " lead_time_days) select id, %s, %s, 30"
                      "  from public.organisations", (sku, desc))

    assert client.post("/api/order", json={
        "po_number": "PO-1", "supplier": "ROCHE", "branch": "KHI",
        "currency": "EUR", "fx_rate": 305,
        "lines": [{"sku": "RGT-1", "qty": 300, "unit_price": 56},
                  {"sku": "RGT-2", "qty": 100, "unit_price": 22},
                  {"sku": "SPR-3", "qty": 20, "unit_price": 410}],
    }).status_code == 200
    assert client.post("/api/record/shipment", json={
        "reference": "SHP-1", "supplier_code": "ROCHE", "mode": "sea",
        "status": "arrived"}).status_code == 200

    yield client, as_owner
    con.close()


def rows(as_owner, sql, args=()):
    with as_owner() as c:
        return c.execute(sql, args).fetchall()


def said(r):
    d = r.json()
    return (d.get("error") or " ".join(d.get("problems") or [])).lower()


# ------------------------------------------- what the page is given


def test_the_page_says_what_this_shipment_could_be_carrying(ready):
    client, _ = ready
    r = client.get("/api/receiving/plan?shipment=SHP-1")
    assert r.status_code == 200, r.text[:400]
    d = r.json()
    assert d["ok"] is True
    assert d["shipment"]["reference"] == "SHP-1"
    got = {l["sku"]: l for l in d["lines"]}
    assert set(got) == {"RGT-1", "RGT-2", "SPR-3"}
    assert got["RGT-1"]["outstanding"] == 300
    assert got["RGT-1"]["po_number"] == "PO-1"
    assert got["RGT-1"]["branch"] == "KHI"
    # So the form can show the price without the person looking it up.
    assert got["RGT-1"]["unit_price"] == 56
    assert got["RGT-1"]["currency"] == "EUR"
    assert got["SPR-3"]["description"] == "Probe"


def test_a_line_already_received_in_full_is_not_offered_again(ready):
    client, _ = ready
    assert client.post("/api/receive", json={
        "sku": "RGT-2", "branch": "KHI", "po": "PO-1", "shipment": "SHP-1",
        "qty": 100, "batch": "B-X", "grn": "GRN-0"}).status_code == 200
    d = client.get("/api/receiving/plan?shipment=SHP-1").json()
    assert "RGT-2" not in {l["sku"] for l in d["lines"]}


def test_a_part_received_line_offers_only_the_rest(ready):
    client, _ = ready
    assert client.post("/api/receive", json={
        "sku": "RGT-1", "branch": "KHI", "po": "PO-1", "shipment": "SHP-1",
        "qty": 120, "batch": "B-X", "grn": "GRN-0"}).status_code == 200
    d = client.get("/api/receiving/plan?shipment=SHP-1").json()
    got = {l["sku"]: l for l in d["lines"]}
    assert got["RGT-1"]["outstanding"] == 180
    assert got["RGT-1"]["received"] == 120


def test_another_suppliers_order_is_not_offered(ready):
    """The same rule the one-line form obeys. Receiving one
    supplier's goods onto another's shipment allocates the wrong
    freight to the wrong inventory, silently."""
    client, as_owner = ready
    with as_owner() as c:
        c.execute("insert into public.suppliers (org_id, code, name)"
                  " select id, 'MIND', 'Mindray' from public.organisations")
    assert client.post("/api/order", json={
        "po_number": "PO-MIND", "supplier": "MIND", "branch": "KHI",
        "currency": "PKR", "fx_rate": 1,
        "lines": [{"sku": "RGT-1", "qty": 50, "unit_price": 9000}],
    }).status_code == 200
    d = client.get("/api/receiving/plan?shipment=SHP-1").json()
    assert "PO-MIND" not in {l["po_number"] for l in d["lines"]}


def test_a_shipment_that_is_not_there_says_so(ready):
    client, _ = ready
    r = client.get("/api/receiving/plan?shipment=SHP-NOPE")
    assert r.status_code == 404
    assert "shp-nope" in said(r)


# ----------------------------------------------- recording the lot


def three_lines(**over):
    base = {
        "shipment": "SHP-1", "grn": "GRN-77",
        "lines": [
            {"sku": "RGT-1", "branch": "KHI", "po": "PO-1", "qty": 300,
             "batch": "B-1", "expiry": "2028-06-30", "weight": 480},
            {"sku": "RGT-2", "branch": "KHI", "po": "PO-1", "qty": 100,
             "batch": "B-2", "weight": 120},
            {"sku": "SPR-3", "branch": "KHI", "po": "PO-1", "qty": 20,
             "batch": "B-3", "weight": 60},
        ],
    }
    base.update(over)
    return base


def test_a_whole_shipment_is_recorded_in_one_go(ready):
    client, as_owner = ready
    r = client.post("/api/receive/many", json=three_lines())
    assert r.status_code == 200, r.text[:400]
    d = r.json()
    assert d["ok"] is True and d["lines"] == 3

    # One document, three lines under it.
    assert len(rows(as_owner, "select 1 from public.goods_receipts")) == 1
    got = {g[0]: float(g[1]) for g in rows(
        as_owner,
        "select i.sku, gl.qty from public.grn_lines_live gl"
        "  join public.items i on i.id = gl.item_id")}
    assert got == {"RGT-1": 300.0, "RGT-2": 100.0, "SPR-3": 20.0}

    # And the order is closed out. The view keeps a row per line and
    # reports what is left on it, so "nothing outstanding" is three
    # zeroes rather than no rows.
    assert [float(r[0]) for r in rows(
        as_owner, "select outstanding from public.po_outstanding")] \
        == [0.0, 0.0, 0.0]


def test_the_whole_lot_is_refused_when_one_line_is_wrong(ready):
    """Half a container recorded is worse than none: the stock is
    wrong in a way that looks right, and nothing says which half is
    missing."""
    client, as_owner = ready
    bad = three_lines()
    bad["lines"][1]["sku"] = "NOPE-9"
    r = client.post("/api/receive/many", json=bad)
    assert r.status_code == 400
    assert "nope-9" in said(r)
    assert rows(as_owner, "select 1 from public.goods_receipts") == []
    assert rows(as_owner, "select 1 from public.stock_movement") == []


def test_the_line_that_failed_is_named_by_its_place(ready):
    """"There is no item NOPE-9" in a form of eight rows leaves
    somebody counting down the list to find which one."""
    client, _ = ready
    bad = three_lines()
    bad["lines"][2]["qty"] = -4
    r = client.post("/api/receive/many", json=bad)
    assert r.status_code == 400
    assert "line 3" in said(r)


def test_an_empty_list_is_refused(ready):
    client, _ = ready
    r = client.post("/api/receive/many", json=three_lines(lines=[]))
    assert r.status_code == 400
    assert "line" in said(r)


def test_every_line_lands_under_one_goods_receipt_number(ready):
    client, as_owner = ready
    assert client.post("/api/receive/many",
                       json=three_lines()).status_code == 200
    refs = rows(as_owner, "select distinct grn_number from"
                " public.goods_receipts")
    assert [r[0] for r in refs] == ["GRN-77"]


def test_the_measurements_reach_the_shipment_lines(ready):
    """So the freight divides properly the first time, instead of
    being skewed until somebody goes back and enters a weight."""
    client, as_owner = ready
    assert client.post("/api/receive/many",
                       json=three_lines()).status_code == 200
    got = {g[0]: float(g[1]) for g in rows(
        as_owner,
        "select i.sku, sl.weight_kg from public.shipment_lines sl"
        "  join public.grn_lines_live gl on gl.id = sl.grn_line_id"
        "  join public.items i on i.id = gl.item_id")}
    assert got == {"RGT-1": 480.0, "RGT-2": 120.0, "SPR-3": 60.0}


def test_the_charges_divide_across_everything_received(ready):
    """The reason to record the container at once rather than a line
    a week: a charge divided by weight needs every line's weight, and
    a shipment half entered gives a skewed landed cost to everything
    on it."""
    client, as_owner = ready
    assert client.post("/api/record/shipment_cost", json={
        "shipment_reference": "SHP-1", "kind": "freight",
        "allocate_by": "weight", "amount": 1000, "currency": "USD",
        "fx_rate": 280}).status_code == 200
    assert client.post("/api/receive/many",
                       json=three_lines()).status_code == 200

    alloc = {g[0]: float(g[1]) for g in rows(
        as_owner,
        "select i.sku, lc.allocated_cost from public.landed_cost lc"
        "  join public.grn_lines_live gl on gl.id = lc.grn_line_id"
        "  join public.items i on i.id = gl.item_id")}
    assert round(sum(alloc.values()), 2) == 280_000.0
    # 480 of 660 kg.
    assert round(alloc["RGT-1"], 2) == round(280_000 * 480 / 660, 2)
    # And nothing is provisional, because every line was measured.
    assert all(r[0] is False for r in rows(
        as_owner, "select provisional from public.landed_cost"))


def test_receiving_the_same_delivery_twice_does_not_double_it(ready):
    """A person presses the button twice, or the connection drops
    after the write and before the answer."""
    client, as_owner = ready
    body = three_lines()
    body["client_ref"] = "once-only"
    first = client.post("/api/receive/many", json=body)
    assert first.status_code == 200, first.text[:300]
    again = client.post("/api/receive/many", json=body)
    assert again.status_code == 200, again.text[:300]

    got = {g[0]: float(g[1]) for g in rows(
        as_owner,
        "select i.sku, sum(gl.qty) from public.grn_lines_live gl"
        "  join public.items i on i.id = gl.item_id group by i.sku")}
    assert got == {"RGT-1": 300.0, "RGT-2": 100.0, "SPR-3": 20.0}
