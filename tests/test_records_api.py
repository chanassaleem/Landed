"""Adding one record at a time, against a real database.

Everything in this product arrived one of two ways until now: a
spreadsheet import, or not at all. These are the routes behind the
popout forms, and what they are really being tested for is their
refusals -- a person filling in a form should never be shown a
constraint name, and every way of getting a field wrong should come
back as a sentence that says which field and what about it.

Reuses the harness in test_people_api.py: the real app, a real
connection, real policies.

    source scripts/dev_postgres.sh
    python3 -m pytest tests/test_records_api.py
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from fastapi.testclient import TestClient            # noqa: E402

import web.app as webapp                             # noqa: E402
from tests.test_people_api import (                  # noqa: E402
    OWNER, OWNER_EMAIL, RealDatabase, schema,        # noqa: F401
)

DSN = os.environ.get("LANDED_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="LANDED_TEST_DSN not set")


@pytest.fixture
def api(schema, monkeypatch):
    schema.execute(
        "insert into auth.users (id, email) values (%s, %s)"
        " on conflict (id) do nothing", (OWNER, OWNER_EMAIL))
    schema.execute("delete from public.organisations")

    from engine.supabase_auth import Claims
    from web import session

    monkeypatch.setattr(
        webapp, "verifier",
        lambda: type("V", (), {"verify": staticmethod(lambda t: Claims(
            user_id=str(OWNER), email=OWNER_EMAIL,
            expires_at=int(time.time()) + 3600))})())

    class FakeAuth(session.Auth):
        def __init__(self):
            super().__init__("https://example.supabase.co", "anon-key")

        def sign_in(self, email, password):
            return session.Tokens("a", "r", 3600)

    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())

    import engine.db.supabase as sup
    monkeypatch.setattr(sup, "database",
                        lambda: RealDatabase(DSN, OWNER_EMAIL))

    client = TestClient(webapp.app)
    client.post("/auth/sign-in", json={"email": OWNER_EMAIL, "password": "x"})
    r = client.post("/api/organisation", json={"name": "Records Co"})
    assert r.status_code == 200, r.text[:300]
    return client, r.json()["id"]


def add(client, what, **body):
    """`what` rather than `kind`, because several records have a column
    called kind and a keyword argument cannot be both."""
    return client.post(f"/api/record/{what}", json=body)


# ------------------------------------------------------------ writing


def test_every_declared_record_can_actually_be_written(api):
    """The table in web/app.py is declarative, so a column that does
    not exist is a runtime error and nothing else catches it.

    Each record is written with only its required fields, because that
    is the shape a person who filled in the minimum will send.
    """
    client, _org = api
    client.post("/api/record/location", json={"code": "KHI",
                                              "name": "Karachi"})
    client.post("/api/record/customer", json={"code": "JPMC",
                                              "name": "Jinnah"})
    client.post("/api/record/item", json={"sku": "RGT-1",
                                          "description": "Reagent"})

    minimal = {
        "supplier": {"code": "ROCHE", "name": "Roche"},
        "commitment": {"label": "Rent", "kind": "rent",
                       "due_date": "2026-12-01", "amount": 320000},
        "cash_balance": {"as_at": "2026-11-30", "balance": 1_250_000},
        "employee": {"employee_no": "E-1", "name": "Imran",
                     "basic_monthly": 150000},
        "opportunity": {"reference": "TND-1", "stage": "quoted",
                        "value": 900000},
        "interaction": {"customer_code": "JPMC",
                        "happened_on": "2026-11-20",
                        "summary": "Talked about the tender"},
    }
    for kind, body in minimal.items():
        r = add(client, kind, **body)
        assert r.status_code == 200, f"{kind}: {r.text[:300]}"
        assert r.json()["ok"] is True
        assert r.json()["id"]


def test_a_code_somebody_knows_is_enough_to_link_a_record(api):
    """An opportunity names its customer and its item by their codes.

    Asking for a uuid would mean finding it first, which is a second
    screen in the middle of filling in a form.
    """
    client, _org = api
    client.post("/api/record/customer", json={"code": "AGA",
                                              "name": "Aga Lab"})
    client.post("/api/record/item", json={"sku": "SPR-9",
                                          "description": "Probe"})
    r = add(client, "opportunity", reference="TND-7", stage="lead",
            value=50000, customer_code="aga", item_sku="spr-9")
    assert r.status_code == 200, r.text[:300]

    with RealDatabase(DSN, OWNER_EMAIL).as_user(OWNER) as conn:
        got = conn.execute(
            "select customer_id is not null, item_id is not null"
            "  from public.opportunities where reference = 'TND-7'"
        ).fetchone()
    assert got == (True, True), "the codes did not resolve to records"


def test_a_balance_for_a_day_already_recorded_is_a_correction(api):
    """Not a second account. Somebody re-entering today's balance is
    fixing a typo, and two rows for one day would be summed."""
    client, _org = api
    add(client, "cash_balance", as_at="2026-11-30", balance=1_000_000)
    add(client, "cash_balance", as_at="2026-11-30", balance=1_450_000)

    with RealDatabase(DSN, OWNER_EMAIL).as_user(OWNER) as conn:
        rows = conn.execute(
            "select balance from public.cash_balances"
            " where as_at = '2026-11-30'").fetchall()
    assert [float(r[0]) for r in rows] == [1_450_000.0]


# ---------------------------------------------------------- refusals


def test_a_missing_required_field_is_named(api):
    client, _org = api
    r = add(client, "commitment")
    assert r.status_code == 400
    problems = r.json()["problems"]
    assert "A label is needed." in problems
    assert "Amount is needed." in problems


def test_the_message_reads_as_a_sentence(api):
    """The server's labels are nouns because they land inside one.

    The form's own labels are conversational -- "What it is" is a good
    field label -- and sharing that string produced "What it is is
    needed."
    """
    client, _org = api
    for message in add(client, "commitment").json()["problems"]:
        assert " is is " not in message
        assert message.endswith(".")
        assert message[0].isupper()


def test_a_value_outside_a_known_set_lists_the_set(api):
    client, _org = api
    r = add(client, "commitment", label="x", kind="bribe",
            due_date="2026-12-01", amount=1)
    assert r.status_code == 400
    assert "lc_settlement" in " ".join(r.json()["problems"])


def test_a_date_that_is_not_a_date_says_what_one_looks_like(api):
    client, _org = api
    r = add(client, "commitment", label="x", kind="rent",
            due_date="next tuesday", amount=1)
    assert r.status_code == 400
    assert "YYYY-MM-DD" in " ".join(r.json()["problems"])


def test_a_rate_typed_as_a_percentage_is_refused_not_converted(api):
    """Somebody typing 5 for five per cent is the commonest way to get
    a payroll figure wrong by twenty times.

    Quietly dividing by a hundred would be the software deciding what
    they meant, and the error would surface months later in a figure
    nobody can explain.
    """
    client, _org = api
    r = add(client, "employee", employee_no="E-2", name="Sana",
            basic_monthly=100000, employer_pf_rate=5)
    assert r.status_code == 400
    assert "0.05" in " ".join(r.json()["problems"])

    ok = add(client, "employee", employee_no="E-2", name="Sana",
             basic_monthly=100000, employer_pf_rate=0.05)
    assert ok.status_code == 200, ok.text[:200]


def test_a_number_that_is_not_a_number_is_named(api):
    client, _org = api
    r = add(client, "commitment", label="x", kind="rent",
            due_date="2026-12-01", amount="lots")
    assert r.status_code == 400
    assert "Amount has to be a number." in r.json()["problems"]


def test_a_duplicate_code_says_which_code(api):
    """`customers_org_id_code_key` means nothing to somebody who
    clicked twice."""
    client, _org = api
    add(client, "customer", code="DUP", name="First")
    r = add(client, "customer", code="DUP", name="Second")
    assert r.status_code == 409
    assert r.json()["problems"] == ["DUP is already in use."]


def test_a_code_that_matches_nothing_is_named(api):
    client, _org = api
    r = add(client, "interaction", customer_code="GHOST",
            happened_on="2026-11-20", summary="hello")
    assert r.status_code == 400
    assert "GHOST" in " ".join(r.json()["problems"])


def test_an_unknown_record_type_is_a_404(api):
    client, _org = api
    r = add(client, "wizard", name="Gandalf")
    assert r.status_code == 404


def test_nothing_is_written_when_a_field_is_refused(api):
    """A refusal has to leave the table exactly as it was.

    The cleaning runs before the insert, so this holds by construction
    -- which is the kind of thing that stays true until somebody moves
    a line, and then stops being true silently.
    """
    client, _org = api
    add(client, "customer", code="KEEP", name="Only one")
    add(client, "customer", code="KEEP", name="Second attempt")
    add(client, "customer", name="No code at all")

    with RealDatabase(DSN, OWNER_EMAIL).as_user(OWNER) as conn:
        rows = conn.execute(
            "select code, name from public.customers order by code"
        ).fetchall()
    assert rows == [("KEEP", "Only one")]


def test_text_is_trimmed_and_codes_are_folded_up(api):
    """So that ' khi ' and 'KHI' are one branch and not two.

    A duplicate branch is not a cosmetic problem: stock is counted per
    branch, and two spellings of one place split a position in half.
    """
    client, _org = api
    add(client, "location", code="  khi  ", name="  Karachi  ")
    r = add(client, "location", code="KHI", name="Karachi again")
    assert r.status_code == 409, "two spellings made two branches"

    with RealDatabase(DSN, OWNER_EMAIL).as_user(OWNER) as conn:
        assert conn.execute(
            "select code, name from public.locations"
        ).fetchall() == [("KHI", "Karachi")]


def test_a_blank_optional_field_is_left_unset_not_stored_empty(api):
    """An empty string in a column is not the same as nothing there.

    A city of '' sorts and groups as a real value, so a list of
    branches by city grows a nameless one.
    """
    client, _org = api
    add(client, "location", code="LHE", name="Lahore", city="   ")
    with RealDatabase(DSN, OWNER_EMAIL).as_user(OWNER) as conn:
        assert conn.execute(
            "select city from public.locations where code = 'LHE'"
        ).fetchone()[0] is None


# ---------------------------------------------------- what the UI offers


def test_every_form_the_browser_offers_has_a_route_behind_it():
    """records.js declares the forms; web/app.py declares the writers.

    A form posting to a route that does not exist is a dialog that
    fails on save, which is the worst moment to find out.
    """
    import pathlib
    import re

    from web.app import RECORDS

    js = (pathlib.Path(__file__).resolve().parent.parent
          / "web" / "static" / "records.js").read_text()
    posted = set(re.findall(r"post: '/api/record/([a-z_]+)'", js))
    assert posted, "no forms found in records.js"
    assert posted <= set(RECORDS), \
        f"forms with no route: {posted - set(RECORDS)}"


def test_every_field_a_form_sends_is_one_the_route_accepts():
    """A field the server ignores is a field somebody fills in for
    nothing, and nothing anywhere says so."""
    import pathlib
    import re

    from web.app import RECORDS

    js = (pathlib.Path(__file__).resolve().parent.parent
          / "web" / "static" / "records.js").read_text()
    for block in re.finditer(
            r"post: '/api/record/([a-z_]+)',\s*fields: \[(.*?)\n      \],",
            js, re.S):
        kind, body = block.group(1), block.group(2)
        spec = RECORDS[kind]
        known = set(spec["cols"]) | set(spec.get("lookup") or {})
        # Only top-level field entries: at eight spaces of indent.
        # Matched loosely, the same pattern also picks up the values
        # inside a select's options list.
        for name in re.findall(r"\n        \['([a-z_]+)',", body):
            assert name in known, f"{kind}.{name} is not accepted"


# ------------------------------------------- correcting a fact in place
#
# The question this answers is where data gets entered on a stock
# screen. There are two acts and they must not share a control:
# correcting a lead time is editing something somebody wrote down, and
# recording that forty units went out is a ledger event that can only
# ever be reversed.


def test_a_fact_about_an_item_can_be_corrected(api):
    client, _org = api
    add(client, "item", sku="RGT-1", description="Reagent")
    r = client.post("/api/item/RGT-1", json={
        "description": "HbA1c reagent kit, 100 test",
        "lead_time_days": 75,
        "list_price": 24500,
    })
    assert r.status_code == 200, r.text[:300]
    assert r.json()["changed"]["lead_time_days"] == 75

    with RealDatabase(DSN, OWNER_EMAIL).as_user(OWNER) as conn:
        got = conn.execute(
            "select description, lead_time_days, list_price"
            "  from public.items where sku = 'RGT-1'").fetchone()
    assert got[0] == "HbA1c reagent kit, 100 test"
    assert float(got[1]) == 75
    assert float(got[2]) == 24500


def test_a_quantity_cannot_be_typed_over(api):
    """The line that matters.

    `on_hand` is the sum of the movements and `unit_cost` is derived
    from them. Typing over either would put a number on screen that no
    movement supports, and the first person to reconcile would find
    stock that exists in the product and nowhere else.
    """
    client, _org = api
    add(client, "item", sku="RGT-2", description="Reagent")
    for field, value in (("on_hand", 999), ("unit_cost", 1),
                         ("position", 5)):
        r = client.post("/api/item/RGT-2", json={field: value})
        assert r.status_code == 400, f"{field} was accepted"
        assert "type over" in " ".join(r.json()["problems"]), \
            r.json()["problems"]


def test_a_field_that_is_not_on_an_item_is_named(api):
    client, _org = api
    add(client, "item", sku="RGT-3", description="Reagent")
    r = client.post("/api/item/RGT-3", json={"colour": "blue"})
    assert r.status_code == 400
    assert "not a field on an item" in " ".join(r.json()["problems"])


def test_a_price_that_is_not_a_number_is_refused(api):
    client, _org = api
    add(client, "item", sku="RGT-4", description="Reagent")
    r = client.post("/api/item/RGT-4", json={"list_price": "lots"})
    assert r.status_code == 400
    assert "number" in " ".join(r.json()["problems"])


def test_an_item_that_does_not_exist_is_a_404(api):
    client, _org = api
    r = client.post("/api/item/NOPE", json={"description": "x"})
    assert r.status_code == 404
    assert "NOPE" in " ".join(r.json()["problems"])


def test_changing_nothing_on_an_item_is_refused(api):
    client, _org = api
    add(client, "item", sku="RGT-5", description="Reagent")
    r = client.post("/api/item/RGT-5", json={})
    assert r.status_code == 400
    assert "Nothing to change" in " ".join(r.json()["problems"])


def test_the_browser_only_offers_the_fields_the_server_accepts():
    """The grid asks the server which columns may be typed into and
    the stock module answers. A list hard-coded in the browser would
    drift, and the drift would show up as a cell that looks editable
    and refuses."""
    from web.modules import EDITABLE_ITEM_FIELDS

    assert "on_hand" not in EDITABLE_ITEM_FIELDS
    assert "unit_cost" not in EDITABLE_ITEM_FIELDS
    assert "stock_value" not in EDITABLE_ITEM_FIELDS
    assert "description" in EDITABLE_ITEM_FIELDS
    assert "list_price" in EDITABLE_ITEM_FIELDS


# --------------------------------------------------- the home screen
#
# Fourteen modules in one request. The things worth pinning are that
# it is one request, that a module which throws does not take the
# screen with it, and that every module has a headline -- a fifteenth
# should not be able to ship as a blank card.


def test_home_answers_for_every_module_the_role_may_see(api):
    client, _org = api
    r = client.get("/api/home")
    assert r.status_code == 200, r.text[:300]
    d = r.json()

    from engine.platform import for_role
    expected = {m.id for m in for_role("owner")}
    assert {c["id"] for c in d["cards"]} == expected
    assert d["sections"], "the cards have nowhere to be grouped"


def test_every_module_can_produce_a_headline():
    """Not whether it does on this data -- whether one is defined.

    A module with no entry returns None for its figure, which renders
    as a permanently blank card nobody notices is broken.
    """
    from engine.platform import MODULES
    from web.modules import HEADLINES

    missing = [m.id for m in MODULES if m.id not in HEADLINES]
    assert not missing, f"no headline defined for {missing}"


def test_a_module_that_throws_does_not_blank_the_screen(api, monkeypatch):
    client, _org = api
    # Stock has to be past "waiting" before it can get as far as
    # throwing, or this tests the short circuit instead of the guard.
    add(client, "item", sku="BRK-1", description="Reagent")
    import web.modules as mods

    real = mods.run

    def explode(module_id, facts):
        if module_id == "stock":
            raise RuntimeError("a record this code did not expect")
        return real(module_id, facts)

    monkeypatch.setattr("web.modules.run", explode)
    r = client.get("/api/home")
    assert r.status_code == 200
    cards = {c["id"]: c for c in r.json()["cards"]}
    assert cards["stock"]["state"] == "broken", cards["stock"]
    assert "did not expect" in cards["stock"]["detail"]
    # and the rest still answered rather than going down with it
    assert all(c["state"] != "broken"
               for k, c in cards.items() if k != "stock")


def test_home_does_not_narrow_to_a_branch_by_default(api):
    """Home is the whole business. A summary narrowed to one branch
    without saying so is not a summary."""
    client, _org = api
    assert client.get("/api/home").json()["branch"] is None
