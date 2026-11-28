"""Money coming in, as rows rather than as a total that drifts.

`invoices.amount_paid` was a stored number with nothing behind it.
Somebody typed over it, or an import moved it, and afterwards there
was no way to answer the questions a collections call actually
turns on: when did they pay, how much, against which invoices, and
is the 400,000 that arrived on Tuesday the one they are talking
about.

This is the problem the stock ledger exists to avoid, applied to
cash. A payment is a row. What it settles is a row. The paid total
is the sum of them, so there is no second number to disagree with
the first.

amount_paid stays, and keeps its meaning: the position as it was
when the book was imported, the way an 'opening' movement is the
stock position when the file was loaded. Everything recorded from
then on is a payment. Replacing it instead would have thrown away
every importer's opening ledger to gain nothing.

Allocation matters as much as the payment. Customers pay round
numbers against three invoices and a credit note, and a payment
that cannot be split is a payment somebody records against the
wrong invoice to make the screen balance.
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
def books(monkeypatch):
    """A customer with three invoices, one part paid on import."""
    import time
    from datetime import date, timedelta

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
                (owner, "pay@books.test"))

    monkeypatch.setattr(
        webapp, "verifier",
        lambda: type("V", (), {"verify": staticmethod(lambda t: Claims(
            user_id=str(owner), email="pay@books.test",
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
                        lambda: RealDatabase(DSN, "pay@books.test"))
    monkeypatch.setenv("LANDED_ENV", "dev")

    client = TestClient(webapp.app)
    assert client.post("/auth/sign-in",
                       json={"email": "pay@books.test",
                             "password": "x"}).status_code == 200
    assert client.post("/api/organisation",
                       json={"name": "Collections Co"}).status_code == 200

    def as_owner():
        return RealDatabase(DSN, "pay@books.test").as_user(owner)

    today = date.today()
    with as_owner() as c:
        c.execute("insert into public.locations (org_id, code, name)"
                  " select id, 'KHI', 'Karachi' from public.organisations")
        c.execute("insert into public.customers (org_id, code, name,"
                  " payment_terms_days) select id, 'SHIFA', 'Shifa', 30"
                  "  from public.organisations")
        for no, days, amount, paid in (("INV-1", 90, 500_000, 100_000),
                                       ("INV-2", 60, 300_000, 0),
                                       ("INV-3", 10, 250_000, 0)):
            c.execute(
                "insert into public.invoices (org_id, customer_id,"
                " location_id, invoice_no, invoice_date, due_date,"
                " amount, amount_paid)"
                " select o.id, cu.id, l.id, %s, %s, %s, %s, %s"
                "  from public.organisations o, public.customers cu,"
                "       public.locations l",
                (no, today - timedelta(days=days),
                 today - timedelta(days=days - 30), amount, paid))
    yield client, as_owner
    con.close()


def rows(as_owner, sql, args=()):
    with as_owner() as c:
        return c.execute(sql, args).fetchall()


def said(r):
    d = r.json()
    return (d.get("error") or " ".join(d.get("problems") or [])).lower()


def settlement(as_owner):
    return {r[0]: (float(r[1]), float(r[2]), float(r[3])) for r in rows(
        as_owner,
        "select invoice_no, amount, paid, outstanding"
        "  from public.invoice_settlement")}


# ------------------------------------------- where it starts from


def test_an_imported_position_is_the_opening_balance(books):
    """amount_paid keeps its meaning: what the book said when it was
    loaded, the way an 'opening' movement is the stock on the shelf
    the day the file arrived."""
    _client, as_owner = books
    assert settlement(as_owner) == {
        "INV-1": (500_000.0, 100_000.0, 400_000.0),
        "INV-2": (300_000.0, 0.0, 300_000.0),
        "INV-3": (250_000.0, 0.0, 250_000.0),
    }


# ----------------------------------------------- taking a payment


def test_a_payment_against_one_invoice_settles_it(books):
    client, as_owner = books
    r = client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 400_000, "received_on": "2026-11-20",
        "reference": "CHQ-9001",
        "allocate": [{"invoice": "INV-1", "amount": 400_000}]})
    assert r.status_code == 200, r.text[:400]
    assert settlement(as_owner)["INV-1"] == (500_000.0, 500_000.0, 0.0)


def test_one_payment_can_settle_several_invoices(books):
    """A customer pays a round number against three invoices. A
    payment that cannot be split is one somebody records against the
    wrong invoice to make the screen balance."""
    client, as_owner = books
    r = client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 700_000, "received_on": "2026-11-20",
        "allocate": [{"invoice": "INV-1", "amount": 400_000},
                     {"invoice": "INV-2", "amount": 300_000}]})
    assert r.status_code == 200, r.text[:400]
    s = settlement(as_owner)
    assert s["INV-1"][2] == 0.0
    assert s["INV-2"][2] == 0.0
    assert s["INV-3"][2] == 250_000.0


def test_what_is_not_allocated_sits_on_account(books):
    """Real, and not an error: a customer pays 500,000 against
    400,000 of invoices because that is what the cheque was for."""
    client, as_owner = books
    r = client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 500_000, "received_on": "2026-11-20",
        "allocate": [{"invoice": "INV-1", "amount": 400_000}]})
    assert r.status_code == 200, r.text[:400]
    assert r.json()["on_account"] == 100_000
    on_account = float(rows(
        as_owner,
        "select unallocated from public.customer_account"
        " where code = 'SHIFA'")[0][0])
    assert on_account == 100_000.0


def test_a_payment_with_no_allocation_is_all_on_account(books):
    client, as_owner = books
    r = client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 250_000,
        "received_on": "2026-11-20"})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["on_account"] == 250_000
    # And nothing has been settled by it.
    assert settlement(as_owner)["INV-3"][2] == 250_000.0


def test_allocating_more_than_the_payment_is_refused(books):
    client, as_owner = books
    r = client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 100_000, "received_on": "2026-11-20",
        "allocate": [{"invoice": "INV-1", "amount": 400_000}]})
    assert r.status_code == 400
    assert "more" in said(r)
    assert rows(as_owner, "select 1 from public.payments") == []


def test_allocating_more_than_an_invoice_owes_is_refused(books):
    """Overpaying one invoice to clear a screen hides the credit
    that is actually owed back to the customer."""
    client, _ = books
    r = client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 900_000, "received_on": "2026-11-20",
        "allocate": [{"invoice": "INV-1", "amount": 900_000}]})
    assert r.status_code == 400
    said_it = said(r)
    assert "inv-1" in said_it and "400,000" in said_it.replace(" ", " ")


def test_paying_the_same_invoice_twice_cannot_overshoot(books):
    client, _ = books
    assert client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 400_000, "received_on": "2026-11-20",
        "allocate": [{"invoice": "INV-1", "amount": 400_000}]
    }).status_code == 200
    again = client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 10_000, "received_on": "2026-11-21",
        "allocate": [{"invoice": "INV-1", "amount": 10_000}]})
    assert again.status_code == 400
    assert "inv-1" in said(again)


def test_an_invoice_of_another_customer_is_refused(books):
    client, as_owner = books
    with as_owner() as c:
        c.execute("insert into public.customers (org_id, code, name)"
                  " select id, 'AGA', 'Aga Khan'"
                  "  from public.organisations")
    r = client.post("/api/payment", json={
        "customer": "AGA", "amount": 100_000, "received_on": "2026-11-20",
        "allocate": [{"invoice": "INV-1", "amount": 100_000}]})
    assert r.status_code == 400
    assert "inv-1" in said(r)


def test_a_payment_of_nothing_is_refused(books):
    client, _ = books
    r = client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 0, "received_on": "2026-11-20"})
    assert r.status_code == 400


def test_a_negative_payment_is_refused(books):
    """A refund is its own thing and is not a payment typed with a
    minus in front of it."""
    client, _ = books
    r = client.post("/api/payment", json={
        "customer": "SHIFA", "amount": -5000, "received_on": "2026-11-20"})
    assert r.status_code == 400


def test_nothing_is_written_when_one_allocation_is_bad(books):
    client, as_owner = books
    r = client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 500_000, "received_on": "2026-11-20",
        "allocate": [{"invoice": "INV-2", "amount": 300_000},
                     {"invoice": "NOPE", "amount": 200_000}]})
    assert r.status_code == 400
    assert rows(as_owner, "select 1 from public.payments") == []
    assert rows(as_owner, "select 1 from public.payment_allocations") == []
    assert settlement(as_owner)["INV-2"][2] == 300_000.0


def test_the_payment_records_who_took_it(books):
    client, as_owner = books
    assert client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 100_000,
        "received_on": "2026-11-20"}).status_code == 200
    assert rows(as_owner, "select created_by from"
                " public.payments")[0][0] is not None


def test_the_same_payment_sent_twice_lands_once(books):
    client, as_owner = books
    body = {"customer": "SHIFA", "amount": 400_000,
            "received_on": "2026-11-20", "client_ref": "cheque-9001",
            "allocate": [{"invoice": "INV-1", "amount": 400_000}]}
    assert client.post("/api/payment", json=body).status_code == 200
    again = client.post("/api/payment", json=body)
    assert again.status_code == 200, again.text[:300]
    assert len(rows(as_owner, "select 1 from public.payments")) == 1
    assert settlement(as_owner)["INV-1"][2] == 0.0


# ----------------------------------- and what the modules read


def test_the_owed_screen_reflects_a_payment(books):
    """The point of recording it. Receivables reads the settlement,
    so a payment taken on the phone shows on the next refresh rather
    than at the next import."""
    client, _ = books
    before = client.get("/api/module/money").json()["data"]
    assert client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 400_000, "received_on": "2026-11-20",
        "allocate": [{"invoice": "INV-1", "amount": 400_000}]
    }).status_code == 200
    after = client.get("/api/module/money").json()["data"]
    assert (after["totals"]["outstanding"]
            == before["totals"]["outstanding"] - 400_000)


def test_a_settled_invoice_leaves_the_overdue_list(books):
    client, _ = books
    assert client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 500_000, "received_on": "2026-11-20",
        "allocate": [{"invoice": "INV-1", "amount": 400_000},
                     {"invoice": "INV-3", "amount": 100_000}]
    }).status_code == 200
    rows_ = client.get("/api/module/money").json()["data"]["rows"]
    owed = {r["invoice_no"]: r for r in rows_ if "invoice_no" in r}
    if "INV-1" in owed:
        assert owed["INV-1"]["outstanding"] == 0


# ------------------------------------------ what the form is given


def test_the_form_can_ask_what_a_customer_owes(books):
    """The Owed screen lists customers, not invoices -- right for
    deciding who to ring, and no help at all to the person taking
    the call, who would otherwise need an invoice number by heart."""
    client, _ = books
    r = client.get("/api/owing?customer=SHIFA")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["name"] == "Shifa"
    got = [(i["invoice_no"], i["outstanding"]) for i in d["invoices"]]
    # Oldest first, which is the order money is applied in unless
    # somebody says otherwise.
    assert got == [("INV-1", 400_000.0), ("INV-2", 300_000.0),
                   ("INV-3", 250_000.0)]


def test_a_settled_invoice_drops_out_of_the_list(books):
    client, _ = books
    assert client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 400_000, "received_on": "2026-11-20",
        "allocate": [{"invoice": "INV-1", "amount": 400_000}]
    }).status_code == 200
    d = client.get("/api/owing?customer=SHIFA").json()
    assert "INV-1" not in {i["invoice_no"] for i in d["invoices"]}


def test_money_on_account_is_reported_with_the_invoices(books):
    """So the form can say "they are 100,000 in credit" rather than
    letting somebody allocate a payment that is already there."""
    client, _ = books
    assert client.post("/api/payment", json={
        "customer": "SHIFA", "amount": 150_000,
        "received_on": "2026-11-20"}).status_code == 200
    assert client.get("/api/owing?customer=SHIFA").json()["on_account"] \
        == 150_000


def test_an_unknown_customer_is_named(books):
    client, _ = books
    r = client.get("/api/owing?customer=NOBODY")
    assert r.status_code == 404
    assert "nobody" in said(r)


RECORDS = (ROOT / "web" / "static" / "records.js").read_text()


def test_the_payment_form_fills_its_allocations_in(books):
    """Driven in Chromium: typing JPMC filled two lines, INV-1001 at
    5,200,000 and INV-1002 at 1,400,000, under a hint reading
    "Jinnah Hospital owes 2 invoices, 6,600,000 in all"."""
    i = RECORDS.index("    payment: {")
    spec = RECORDS[i:RECORDS.index("\n    customer: {", i)]
    assert "wire:" in spec, "the form asks the server nothing"
    assert "/api/owing" in spec
    assert "sits on" in spec, (
        "nothing tells the person what happens to the remainder")
