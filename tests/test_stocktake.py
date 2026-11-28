"""Counting a branch as one piece of work.

A count adjustment already existed and is sound: the person types
what is on the shelf, the ledger reconciles to it, and a lock stops
two counts racing. What did not exist was the thing around it.

A stocktake is a morning's work. Somebody walks the shelves with a
list, and the figures they write down are compared against what the
system believed at the moment they started -- not against what it
believes now, because by then the afternoon's sales have gone
through and every variance is wrong by a day's trading.

So the session captures the expected figure when it opens, holds
the counts while they are being taken, shows the variances before
anything is written, and posts the lot as adjustments together.
Until it is posted nothing has touched the ledger, which is what
makes it safe to count over several hours and stop for lunch.

The one case worth care: stock that moves between the count and the
posting. The physical count still wins -- that is what a stocktake
is -- but the line says it moved, because a variance explained by a
sale is a different conversation from a variance that is not.
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
def shelf(monkeypatch):
    """A branch with three items on it."""
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
                (owner, "count@shelf.test"))

    monkeypatch.setattr(
        webapp, "verifier",
        lambda: type("V", (), {"verify": staticmethod(lambda t: Claims(
            user_id=str(owner), email="count@shelf.test",
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
                        lambda: RealDatabase(DSN, "count@shelf.test"))
    monkeypatch.setenv("LANDED_ENV", "dev")

    client = TestClient(webapp.app)
    assert client.post("/auth/sign-in",
                       json={"email": "count@shelf.test",
                             "password": "x"}).status_code == 200
    assert client.post("/api/organisation",
                       json={"name": "Stocktake Co"}).status_code == 200

    def as_owner():
        return RealDatabase(DSN, "count@shelf.test").as_user(owner)

    with as_owner() as c:
        for code, name in (("KHI", "Karachi"), ("LHE", "Lahore")):
            c.execute("insert into public.locations (org_id, code, name)"
                      " select id, %s, %s from public.organisations",
                      (code, name))
        for sku, desc, qty in (("RGT-1", "Reagent", 100),
                               ("RGT-2", "Buffer", 40),
                               ("SPR-3", "Probe", 8)):
            c.execute("insert into public.items (org_id, sku, description,"
                      " lead_time_days) select id, %s, %s, 30"
                      "  from public.organisations", (sku, desc))
            c.execute(
                "insert into public.stock_movement (org_id, item_id,"
                " location_id, qty, unit_cost, reason, occurred_at)"
                " select o.id, i.id, l.id, %s, 500, 'opening', current_date"
                "  from public.organisations o, public.items i,"
                "       public.locations l"
                " where i.sku = %s and l.code = 'KHI'", (qty, sku))
    yield client, as_owner
    con.close()


def rows(as_owner, sql, args=()):
    with as_owner() as c:
        return c.execute(sql, args).fetchall()


def said(r):
    d = r.json()
    return (d.get("error") or " ".join(d.get("problems") or [])).lower()


def on_hand(as_owner):
    return {r[0]: float(r[1]) for r in rows(
        as_owner,
        "select i.sku, s.on_hand from public.stock_on_hand s"
        "  join public.items i on i.id = s.item_id")}


# --------------------------------------------------- opening one


def test_opening_a_count_lists_what_the_branch_should_hold(shelf):
    client, _ = shelf
    r = client.post("/api/stocktake", json={"branch": "KHI",
                                            "reference": "ST-1"})
    assert r.status_code == 200, r.text[:400]
    d = r.json()
    assert d["ok"] is True
    assert d["reference"] == "ST-1"
    assert {l["sku"]: l["expected"] for l in d["lines"]} == {
        "RGT-1": 100.0, "RGT-2": 40.0, "SPR-3": 8.0}
    # Nothing is counted yet, which is different from counted as zero.
    assert all(l["counted"] is None for l in d["lines"])


def test_nothing_is_written_to_the_ledger_by_opening_one(shelf):
    """A count takes a morning. Until it is posted the shelf figure
    has to be the one everybody else is still selling against."""
    client, as_owner = shelf
    assert client.post("/api/stocktake",
                       json={"branch": "KHI"}).status_code == 200
    assert on_hand(as_owner) == {"RGT-1": 100.0, "RGT-2": 40.0,
                                 "SPR-3": 8.0}


def test_the_expected_figure_is_captured_at_the_moment_it_opens(shelf):
    """Against what the system believed when the counter started --
    not what it believes now, because by then the afternoon's sales
    have gone through and every variance is wrong by a day of
    trading."""
    client, as_owner = shelf
    d = client.post("/api/stocktake", json={"branch": "KHI"}).json()
    ref = d["reference"]
    # The day carries on around the count.
    # kind, and a positive quantity: the sign comes from the kind,
    # deliberately, so a form cannot send a negative sale.
    assert client.post("/api/movement", json={
        "sku": "RGT-1", "branch": "KHI", "qty": 30,
        "kind": "sale"}).status_code == 200
    again = client.get(f"/api/stocktake/{ref}").json()
    held = {l["sku"]: l["expected"] for l in again["lines"]}
    assert held["RGT-1"] == 100.0, (
        "the expected figure moved under the counter")


def test_a_second_count_of_the_same_branch_is_refused(shelf):
    """Two open counts of one branch is two people writing different
    numbers against the same shelf."""
    client, _ = shelf
    assert client.post("/api/stocktake",
                       json={"branch": "KHI"}).status_code == 200
    again = client.post("/api/stocktake", json={"branch": "KHI"})
    assert again.status_code == 400
    assert "already" in said(again)


def test_another_branch_can_be_counted_at_the_same_time(shelf):
    client, _ = shelf
    assert client.post("/api/stocktake",
                       json={"branch": "KHI"}).status_code == 200
    assert client.post("/api/stocktake",
                       json={"branch": "LHE"}).status_code == 200


def test_a_branch_with_nothing_on_it_can_still_be_counted(shelf):
    """An empty shelf is a finding, and a count that refuses to open
    is a count nobody can record it with."""
    client, _ = shelf
    r = client.post("/api/stocktake", json={"branch": "LHE"})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["lines"] == []


# ------------------------------------------------ taking the count


def test_counts_are_held_without_touching_the_ledger(shelf):
    client, as_owner = shelf
    ref = client.post("/api/stocktake",
                      json={"branch": "KHI"}).json()["reference"]
    r = client.post(f"/api/stocktake/{ref}/count", json={
        "counts": [{"sku": "RGT-1", "counted": 97},
                   {"sku": "SPR-3", "counted": 8}]})
    assert r.status_code == 200, r.text[:400]
    assert on_hand(as_owner)["RGT-1"] == 100.0

    d = client.get(f"/api/stocktake/{ref}").json()
    got = {l["sku"]: (l["expected"], l["counted"], l["variance"])
           for l in d["lines"]}
    assert got["RGT-1"] == (100.0, 97.0, -3.0)
    assert got["SPR-3"] == (8.0, 8.0, 0.0)
    assert got["RGT-2"][1] is None


def test_a_count_can_be_typed_again_before_posting(shelf):
    """Somebody miscounts, recounts, and types it again. That is the
    whole reason for holding them."""
    client, _ = shelf
    ref = client.post("/api/stocktake",
                      json={"branch": "KHI"}).json()["reference"]
    client.post(f"/api/stocktake/{ref}/count",
                json={"counts": [{"sku": "RGT-1", "counted": 97}]})
    client.post(f"/api/stocktake/{ref}/count",
                json={"counts": [{"sku": "RGT-1", "counted": 99}]})
    d = client.get(f"/api/stocktake/{ref}").json()
    line = [l for l in d["lines"] if l["sku"] == "RGT-1"][0]
    assert line["counted"] == 99.0


def test_a_count_of_zero_is_a_count(shelf):
    """An empty shelf for an item the ledger says has eight on it is
    the single most valuable thing a stocktake finds."""
    client, _ = shelf
    ref = client.post("/api/stocktake",
                      json={"branch": "KHI"}).json()["reference"]
    client.post(f"/api/stocktake/{ref}/count",
                json={"counts": [{"sku": "SPR-3", "counted": 0}]})
    d = client.get(f"/api/stocktake/{ref}").json()
    line = [l for l in d["lines"] if l["sku"] == "SPR-3"][0]
    assert line["counted"] == 0.0
    assert line["variance"] == -8.0


def test_a_negative_count_is_refused(shelf):
    client, _ = shelf
    ref = client.post("/api/stocktake",
                      json={"branch": "KHI"}).json()["reference"]
    r = client.post(f"/api/stocktake/{ref}/count",
                    json={"counts": [{"sku": "RGT-1", "counted": -5}]})
    assert r.status_code == 400


def test_an_item_that_is_not_on_the_list_is_added(shelf):
    """Stock turns up that the system did not know was there, which
    is as real a finding as stock that is missing."""
    client, as_owner = shelf
    with as_owner() as c:
        c.execute("insert into public.items (org_id, sku, description,"
                  " lead_time_days) select id, 'NEW-9', 'Found', 30"
                  "  from public.organisations")
    ref = client.post("/api/stocktake",
                      json={"branch": "KHI"}).json()["reference"]
    r = client.post(f"/api/stocktake/{ref}/count",
                    json={"counts": [{"sku": "NEW-9", "counted": 12}]})
    assert r.status_code == 200, r.text[:300]
    d = client.get(f"/api/stocktake/{ref}").json()
    line = [l for l in d["lines"] if l["sku"] == "NEW-9"][0]
    assert (line["expected"], line["counted"]) == (0.0, 12.0)


# -------------------------------------------------- posting it


def test_posting_writes_every_counted_line_at_once(shelf):
    client, as_owner = shelf
    ref = client.post("/api/stocktake",
                      json={"branch": "KHI"}).json()["reference"]
    client.post(f"/api/stocktake/{ref}/count", json={
        "counts": [{"sku": "RGT-1", "counted": 97},
                   {"sku": "RGT-2", "counted": 44},
                   {"sku": "SPR-3", "counted": 0}]})
    r = client.post(f"/api/stocktake/{ref}/post", json={})
    assert r.status_code == 200, r.text[:400]
    assert r.json()["adjusted"] == 3
    assert on_hand(as_owner) == {"RGT-1": 97.0, "RGT-2": 44.0,
                                 "SPR-3": 0.0}


def test_a_line_nobody_counted_is_left_alone(shelf):
    """Not counted and counted as zero are different facts, and
    treating the first as the second writes off stock nobody looked
    at."""
    client, as_owner = shelf
    ref = client.post("/api/stocktake",
                      json={"branch": "KHI"}).json()["reference"]
    client.post(f"/api/stocktake/{ref}/count",
                json={"counts": [{"sku": "RGT-1", "counted": 97}]})
    client.post(f"/api/stocktake/{ref}/post", json={})
    assert on_hand(as_owner)["RGT-2"] == 40.0
    assert on_hand(as_owner)["SPR-3"] == 8.0


def test_the_adjustments_say_which_count_they_came_from(shelf):
    client, as_owner = shelf
    ref = client.post("/api/stocktake",
                      json={"branch": "KHI"}).json()["reference"]
    client.post(f"/api/stocktake/{ref}/count",
                json={"counts": [{"sku": "RGT-1", "counted": 97}]})
    client.post(f"/api/stocktake/{ref}/post", json={})
    # 'adjustment', which is what a count writes: the ledger's kinds
    # describe what happened to the stock, and what happened is that
    # it was adjusted to a figure somebody read off a shelf.
    note = rows(as_owner,
                "select note from public.stock_movement"
                " where reason = 'adjustment' limit 1")[0][0]
    assert ref in (note or "")


def test_a_posted_count_cannot_be_posted_again(shelf):
    client, as_owner = shelf
    ref = client.post("/api/stocktake",
                      json={"branch": "KHI"}).json()["reference"]
    client.post(f"/api/stocktake/{ref}/count",
                json={"counts": [{"sku": "RGT-1", "counted": 97}]})
    assert client.post(f"/api/stocktake/{ref}/post",
                       json={}).status_code == 200
    again = client.post(f"/api/stocktake/{ref}/post", json={})
    assert again.status_code == 400
    assert "posted" in said(again)
    assert on_hand(as_owner)["RGT-1"] == 97.0


def test_a_posted_count_cannot_be_typed_into(shelf):
    client, _ = shelf
    ref = client.post("/api/stocktake",
                      json={"branch": "KHI"}).json()["reference"]
    client.post(f"/api/stocktake/{ref}/count",
                json={"counts": [{"sku": "RGT-1", "counted": 97}]})
    client.post(f"/api/stocktake/{ref}/post", json={})
    r = client.post(f"/api/stocktake/{ref}/count",
                    json={"counts": [{"sku": "RGT-2", "counted": 1}]})
    assert r.status_code == 400


def test_posting_nothing_is_refused(shelf):
    client, _ = shelf
    ref = client.post("/api/stocktake",
                      json={"branch": "KHI"}).json()["reference"]
    r = client.post(f"/api/stocktake/{ref}/post", json={})
    assert r.status_code == 400
    assert "counted" in said(r)


def test_stock_that_moved_since_it_was_counted_is_flagged(shelf):
    """The physical count still wins -- that is what a stocktake is
    -- but a variance explained by a sale is a different
    conversation from one that is not."""
    client, _ = shelf
    ref = client.post("/api/stocktake",
                      json={"branch": "KHI"}).json()["reference"]
    client.post(f"/api/stocktake/{ref}/count",
                json={"counts": [{"sku": "RGT-1", "counted": 97}]})
    assert client.post("/api/movement", json={
        "sku": "RGT-1", "branch": "KHI", "qty": 10,
        "kind": "sale"}).status_code == 200
    d = client.get(f"/api/stocktake/{ref}").json()
    line = [l for l in d["lines"] if l["sku"] == "RGT-1"][0]
    assert line["moved_since"] is True
    assert line["on_hand_now"] == 90.0


def test_abandoning_a_count_leaves_the_ledger_alone(shelf):
    client, as_owner = shelf
    ref = client.post("/api/stocktake",
                      json={"branch": "KHI"}).json()["reference"]
    client.post(f"/api/stocktake/{ref}/count",
                json={"counts": [{"sku": "RGT-1", "counted": 2}]})
    r = client.post(f"/api/stocktake/{ref}/abandon", json={})
    assert r.status_code == 200, r.text[:300]
    assert on_hand(as_owner)["RGT-1"] == 100.0
    # And the branch can be counted again.
    assert client.post("/api/stocktake",
                       json={"branch": "KHI"}).status_code == 200


def test_the_open_counts_can_be_listed(shelf):
    client, _ = shelf
    client.post("/api/stocktake", json={"branch": "KHI"})
    r = client.get("/api/stocktake")
    assert r.status_code == 200, r.text[:300]
    open_ = [s for s in r.json()["stocktakes"] if s["status"] == "open"]
    assert len(open_) == 1
    assert open_[0]["branch"] == "KHI"
