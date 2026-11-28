"""Turning a list of findings into work that gets done.

Checks produced a good list and no way to act on it. Two people
look at the same twelve findings, each assumes the other is on the
third one, and the list reads identically next week. A finding
nobody can pick up is a finding nobody picks up.

Two things are needed and they are different. Assigning says who is
on it. Accepting says the business has looked and this is how
things are -- a branch really does hold one item with no cost
because it is a free sample, and reporting it every week trains
people to scroll past the list.

Resolution is not stored, on purpose. A finding is computed from
the data; when the data is fixed the check stops producing it.
Storing "resolved" would create a second answer to the question
"is this still wrong", and the stored one would be wrong first.

Acceptance is the one that needs care. Accepting "items with no
unit cost" and then adding a new item with no cost must not hide
the new one -- that is how an accepted finding becomes a blindfold.
So an acceptance records the rows it covered, and a finding that
names a row outside that set comes back.
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
def checks(monkeypatch):
    """An organisation with something wrong in it."""
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
                (owner, "checks@fix.test"))

    monkeypatch.setattr(
        webapp, "verifier",
        lambda: type("V", (), {"verify": staticmethod(lambda t: Claims(
            user_id=str(owner), email="checks@fix.test",
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
                        lambda: RealDatabase(DSN, "checks@fix.test"))
    monkeypatch.setenv("LANDED_ENV", "dev")

    client = TestClient(webapp.app)
    assert client.post("/auth/sign-in",
                       json={"email": "checks@fix.test",
                             "password": "x"}).status_code == 200
    assert client.post("/api/organisation",
                       json={"name": "Checks Co"}).status_code == 200

    def as_owner():
        return RealDatabase(DSN, "checks@fix.test").as_user(owner)

    with as_owner() as c:
        c.execute("insert into public.locations (org_id, code, name)"
                  " select id, 'KHI', 'Karachi' from public.organisations")
        # Two items with no cost behind them: a real finding.
        for sku in ("FREE-1", "FREE-2"):
            c.execute("insert into public.items (org_id, sku, description,"
                      " lead_time_days) select id, %s, 'Sample', 30"
                      "  from public.organisations", (sku,))
            c.execute(
                "insert into public.stock_movement (org_id, item_id,"
                " location_id, qty, unit_cost, reason, occurred_at)"
                " select o.id, i.id, l.id, 5, 0, 'opening', current_date"
                "  from public.organisations o, public.items i,"
                "       public.locations l where i.sku = %s", (sku,))
    yield client, as_owner, str(owner)
    con.close()


def said(r):
    d = r.json()
    return (d.get("error") or " ".join(d.get("problems") or [])).lower()


def findings(client):
    d = client.get("/api/module/quality").json()["data"]
    return {f["code"]: f for f in d["rows"]}


# ----------------------------------------------- who is on it


def test_a_finding_can_be_given_to_somebody(checks):
    client, _as_owner, owner = checks
    code = next(iter(findings(client)))
    r = client.post("/api/check/assign", json={
        "code": code, "to": owner, "note": "Chasing the supplier"})
    assert r.status_code == 200, r.text[:400]
    got = findings(client)[code]
    assert got["assigned_to_email"] == "checks@fix.test"
    assert got["disposition_note"] == "Chasing the supplier"
    assert got["state"] == "assigned"


def test_an_unassigned_finding_says_so_rather_than_nothing(checks):
    client, _a, _o = checks
    got = next(iter(findings(client).values()))
    assert got["state"] == "open"
    assert got["assigned_to_email"] is None


def test_it_can_be_handed_back(checks):
    client, _a, owner = checks
    code = next(iter(findings(client)))
    client.post("/api/check/assign", json={"code": code, "to": owner})
    r = client.post("/api/check/assign", json={"code": code, "to": ""})
    assert r.status_code == 200, r.text[:300]
    assert findings(client)[code]["state"] == "open"


def test_a_person_outside_the_business_cannot_be_given_one(checks):
    client, _a, _o = checks
    code = next(iter(findings(client)))
    r = client.post("/api/check/assign", json={
        "code": code, "to": str(uuid.uuid4())})
    assert r.status_code == 400
    assert "not" in said(r)


def test_a_code_no_check_produces_is_refused(checks):
    """A typo here would sit in the table forever, matching nothing
    and explaining nothing."""
    client, _a, owner = checks
    r = client.post("/api/check/assign", json={
        "code": "no_such_check", "to": owner})
    assert r.status_code == 400
    assert "no_such_check" in said(r)


# --------------------------------------------- we have looked


def test_an_accepted_finding_stops_asking(checks):
    client, _a, _o = checks
    code = "unit_cost_zero"
    assert code in findings(client), "the fixture no longer trips it"
    r = client.post("/api/check/accept", json={
        "code": code, "why": "Both are free samples from the supplier."})
    assert r.status_code == 200, r.text[:400]
    got = findings(client)[code]
    assert got["state"] == "accepted"
    assert got["disposition_note"].startswith("Both are free")


def test_accepting_needs_a_reason(checks):
    """"We know" without why is a finding somebody silences and
    nobody can audit."""
    client, _a, _o = checks
    r = client.post("/api/check/accept", json={"code": "unit_cost_zero"})
    assert r.status_code == 400
    assert "why" in said(r)


def test_an_accepted_finding_is_not_counted_as_needing_action(checks):
    client, _a, _o = checks
    before = client.get("/api/module/quality").json()["data"]["totals"]
    client.post("/api/check/accept", json={
        "code": "unit_cost_zero", "why": "Free samples."})
    after = client.get("/api/module/quality").json()["data"]["totals"]
    assert after["needing_action"] < before["total"]
    assert after["accepted"] == 1
    # Still listed, because hiding it is how it is forgotten.
    assert "unit_cost_zero" in findings(client)


def test_a_new_row_brings_an_accepted_finding_back(checks):
    """The important one. Accepting "items with no unit cost" and
    then adding a new one must not hide the new one -- that is how
    an accepted finding becomes a blindfold."""
    client, as_owner, _o = checks
    client.post("/api/check/accept", json={
        "code": "unit_cost_zero", "why": "FREE-1 and FREE-2 are samples."})
    assert findings(client)["unit_cost_zero"]["state"] == "accepted"

    with as_owner() as c:
        c.execute("insert into public.items (org_id, sku, description,"
                  " lead_time_days) select id, 'OOPS-3', 'Real item', 30"
                  "  from public.organisations")
        c.execute(
            "insert into public.stock_movement (org_id, item_id,"
            " location_id, qty, unit_cost, reason, occurred_at)"
            " select o.id, i.id, l.id, 9, 0, 'opening', current_date"
            "  from public.organisations o, public.items i,"
            "       public.locations l where i.sku = 'OOPS-3'")

    got = findings(client)["unit_cost_zero"]
    assert got["state"] == "open", (
        "a row nobody accepted is hidden behind an old acceptance")
    # The row keys carry their branch -- "OOPS-3 at default/KHI" --
    # so the finding can be acted on without a second lookup.
    assert any("OOPS-3" in r for r in (got.get("unaccepted_rows") or [])), (
        got.get("unaccepted_rows"))


def test_the_same_rows_stay_accepted(checks):
    """Re-running the check must not reopen it just because it ran
    again."""
    client, _a, _o = checks
    client.post("/api/check/accept", json={
        "code": "unit_cost_zero", "why": "Samples."})
    for _ in range(3):
        assert findings(client)["unit_cost_zero"]["state"] == "accepted"


def test_an_acceptance_can_be_taken_back(checks):
    client, _a, _o = checks
    client.post("/api/check/accept", json={
        "code": "unit_cost_zero", "why": "Samples."})
    r = client.post("/api/check/reopen", json={"code": "unit_cost_zero"})
    assert r.status_code == 200, r.text[:300]
    assert findings(client)["unit_cost_zero"]["state"] == "open"


def test_who_decided_and_when_is_kept(checks):
    client, as_owner, _o = checks
    client.post("/api/check/accept", json={
        "code": "unit_cost_zero", "why": "Samples."})
    with as_owner() as c:
        row = c.execute(
            "select decided_by, decided_at, why from"
            " public.check_dispositions where code = 'unit_cost_zero'"
        ).fetchone()
    assert row[0] is not None and row[1] is not None
    assert row[2] == "Samples."


def test_a_resolution_is_not_stored(checks):
    """A finding is computed from the data. When the data is fixed
    the check stops producing it, and a stored "resolved" would be
    a second answer to "is this still wrong" that goes stale
    first."""
    with open(ROOT / "supabase" / "migrations"
              / "0029_a_finding_somebody_owns.sql") as f:
        sql = f.read()
    assert "'resolved'" not in sql, (
        "resolution is being stored, which creates a second answer")


# ------------------------------------------------ on the screen
#
# Read from the source: what the browser does with a grid spec has
# no endpoint of its own. Driven in Chromium against a real
# database -- Take put "assigned / owner@seed.test" on the row,
# Accept put "accepted" on it, and the tiles read "1 Left to pick
# up / 1 Somebody is on / 0 Accepted as is".

PLATFORM = (ROOT / "web" / "static" / "platform.js").read_text()
GRID = (ROOT / "web" / "static" / "grid.js").read_text()


def test_a_finding_can_be_acted_on_without_leaving_the_list():
    i = PLATFORM.index("      rowActions: [")
    block = PLATFORM[i:PLATFORM.index("      ],", i)]
    for verb in ("Take", "Hand back", "Accept", "Reopen"):
        assert f"'{verb}'" in block, f"no way to {verb.lower()} a finding"


def test_accepting_asks_why_before_it_will_do_it():
    i = PLATFORM.index("async function settleCheck(")
    body = PLATFORM[i:PLATFORM.index("\n  }", i)]
    assert "window.prompt" in body
    assert "if (!why || !why.trim()) return;" in body, (
        "an empty reason still accepts the finding")


def test_the_tile_counts_what_is_left_to_pick_up():
    """A list of twelve that includes four nobody has to do anything
    about reads as twelve jobs."""
    i = PLATFORM.index("      tiles: [", PLATFORM.index("    quality: {"))
    block = PLATFORM[i:PLATFORM.index("      ],", i)]
    assert "'needing_action', 'Left to pick up'" in block


def test_the_provisional_glyph_is_only_put_on_numbers():
    """The tilde means approximate. "~Items with no unit cost" reads
    as a typo, and "~--" as approximately nothing."""
    i = GRID.index("td.classList.add('unsure')")
    branch = GRID[i:i + 900]
    assert "numeric.has(c.fmt)" in branch
