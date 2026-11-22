"""The people routes, against a real database and real policies.

tests/test_invites.py proves the schema behaves: the trigger accepts a
live invite, the guards refuse an expired one, an owner cannot lock
themselves out. What it cannot prove is that the routes on top of it
agree -- that a manager is refused before the policy has to refuse
them, that an address is folded on the way in, that a second invite to
the same person comes back as a sentence rather than a constraint name.

So this wires the real app to a real connection, acting as a real
signed-in user, with row-level security on. A route that works only
because the test mocked the database away would pass elsewhere and
fail here.

    source scripts/dev_postgres.sh
    python3 -m pytest tests/test_people_api.py
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import time
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from fastapi.testclient import TestClient            # noqa: E402

import web.app as webapp                             # noqa: E402

DSN = os.environ.get("LANDED_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="LANDED_TEST_DSN not set")

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
SHIM = ROOT / "supabase" / "tests" / "00_supabase_shim.sql"

OWNER = uuid.UUID("44444444-4444-4444-4444-444444444444")
OWNER_EMAIL = "owner@people.test"


@pytest.fixture(scope="module")
def schema():
    con = psycopg.connect(DSN, autocommit=True)
    for s in ("public", "app", "auth"):
        con.execute(f"drop schema if exists {s} cascade")
    con.execute("create schema public")
    for path in [SHIM, *MIGRATIONS]:
        con.execute(path.read_text())
    yield con
    con.close()


class RealDatabase:
    """What `engine.db.supabase.database()` returns, over real Postgres.

    `as_user` sets the role and the JWT claims for the length of the
    block, which is the same mechanism Supabase uses underneath -- so
    the policies apply exactly as they do in production, and a route
    that quietly depends on being the table owner fails.
    """

    def __init__(self, dsn, email):
        self.dsn, self.email = dsn, email

    @contextlib.contextmanager
    def as_user(self, user_id):
        con = psycopg.connect(self.dsn, autocommit=True)
        try:
            with con.transaction():
                con.execute("set local role authenticated")
                con.execute(
                    "select set_config('request.jwt.claims', %s, true)",
                    (json.dumps({"sub": str(user_id),
                                 "role": "authenticated",
                                 "email": self.email}),))
                yield con
        finally:
            con.close()


@pytest.fixture
def api(schema, monkeypatch):
    """A signed-in owner of a fresh organisation, talking to the app."""
    schema.execute(
        "insert into auth.users (id, email) values (%s, %s)"
        " on conflict (id) do nothing", (OWNER, OWNER_EMAIL))

    # A new organisation per test, and the newest is the one the routes
    # pick: they all select `order by created_at limit 1`, so the
    # oldest actually wins. Cleared instead, which keeps each test
    # independent of the ones before it.
    schema.execute("delete from public.organisations")

    from engine.supabase_auth import Claims

    monkeypatch.setattr(
        webapp, "verifier",
        lambda: type("V", (), {"verify": staticmethod(lambda t: Claims(
            user_id=str(OWNER), email=OWNER_EMAIL,
            expires_at=int(time.time()) + 3600))})())

    from web import session

    class FakeAuth(session.Auth):
        """Subclassed rather than duck-typed: the route asks the auth
        object whether it is configured before it asks it to sign
        anyone in, so a bare stand-in fails on an attribute instead of
        on anything this file is about."""

        def __init__(self):
            super().__init__("https://example.supabase.co", "anon-key")

        def sign_in(self, email, password):
            return session.Tokens("access-token", "refresh-token", 3600)

    monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())

    import engine.db.supabase as sup
    monkeypatch.setattr(sup, "database",
                        lambda: RealDatabase(DSN, OWNER_EMAIL))

    client = TestClient(webapp.app)
    assert client.post("/auth/sign-in",
                       json={"email": OWNER_EMAIL,
                             "password": "x"}).status_code == 200
    r = client.post("/api/organisation", json={"name": "People Co"})
    assert r.status_code == 200, r.text[:300]
    org = r.json()["id"]
    with RealDatabase(DSN, OWNER_EMAIL).as_user(OWNER) as conn:
        loc = conn.execute(
            "insert into public.locations (org_id, code, name)"
            " values (%s,'KHI','Karachi') returning id", (org,)).fetchone()[0]
    return client, org, str(loc)


# ----------------------------------------------------------- the list


def test_a_new_organisation_has_one_member_and_no_invites(api):
    client, _org, _loc = api
    d = client.get("/api/users").json()
    assert len(d["members"]) == 1
    assert d["members"][0]["role"] == "owner"
    assert d["members"][0]["is_you"] is True
    assert d["invites"] == []
    assert d["can_invite"] is True and d["can_change"] is True


def test_the_roles_offered_carry_what_each_one_can_open(api):
    """Picking a role is picking which modules somebody sees, so the
    list says so rather than leaving it to be discovered."""
    client, _org, _loc = api
    roles = {r["id"]: r for r in client.get("/api/users").json()["roles"]}
    assert set(roles) == {"owner", "manager", "staff", "accountant"}
    assert "books" in roles["accountant"]["modules"]
    assert "books" not in roles["manager"]["modules"]
    assert "people" not in roles["staff"]["modules"]
    for r in roles.values():
        assert r["blurb"]


# --------------------------------------------------------- inviting


def test_an_invite_is_written_and_says_no_email_was_sent(api):
    """The one thing somebody has to be told, because nothing else will
    tell them: sending mail needs the admin key, which this app does
    not hold at request time."""
    client, _org, loc = api
    r = client.post("/api/invite", json={
        "email": "newbie@people.test", "role": "manager",
        "locations": [loc]})
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["ok"] is True
    assert "No email was sent" in d["note"]
    assert d["expires"]

    listed = client.get("/api/users").json()["invites"]
    assert [i["email"] for i in listed] == ["newbie@people.test"]
    assert listed[0]["role"] == "manager"
    assert listed[0]["locations"] == [loc]
    assert listed[0]["expired"] is False


def test_the_address_is_folded_on_the_way_in(api):
    """GoTrue does not normalise it, so an unfolded invite would be
    invisible to the trigger: the person signs up, nothing happens, and
    there is no error anywhere to explain why they have a login and no
    organisation. The schema refuses an unfolded row, so without the
    fold here this is a 400 instead of an invite.
    """
    client, _org, _loc = api
    r = client.post("/api/invite", json={"email": "  MiXeD@People.TEST ",
                                         "role": "staff"})
    assert r.status_code == 200, r.text[:300]
    assert client.get("/api/users").json()["invites"][0]["email"] \
        == "mixed@people.test"


def test_a_second_invite_to_the_same_person_is_a_sentence(api):
    """Not a constraint name. `invites_live_idx` means nothing to
    somebody who clicked twice."""
    client, _org, _loc = api
    assert client.post("/api/invite", json={
        "email": "dup@people.test", "role": "staff"}).status_code == 200
    r = client.post("/api/invite", json={
        "email": "dup@people.test", "role": "manager"})
    assert r.status_code == 409
    problems = " ".join(r.json()["problems"])
    assert "already been invited" in problems
    assert "idx" not in problems


def test_a_bad_address_and_a_bad_role_are_both_named(api):
    client, _org, _loc = api
    r = client.post("/api/invite", json={"email": "not-an-address",
                                         "role": "wizard"})
    assert r.status_code == 400
    problems = " ".join(r.json()["problems"])
    assert "email address" in problems
    assert "Role has to be" in problems


def test_an_invite_with_no_branches_means_every_branch(api):
    """One column covers the single-shop owner and the multi-branch
    manager, so an empty list is not a mistake to refuse."""
    client, _org, _loc = api
    assert client.post("/api/invite", json={
        "email": "all@people.test", "role": "manager"}).status_code == 200
    assert client.get("/api/users").json()["invites"][0]["locations"] == []


# --------------------------------------------------------- withdrawing


def test_an_invite_can_be_withdrawn_and_then_reissued(api):
    """Revoked rather than deleted, so the record of having invited
    somebody survives withdrawing it -- and the live-invite index is
    scoped to unrevoked rows, so the address is free again."""
    client, _org, _loc = api
    invite = client.post("/api/invite", json={
        "email": "gone@people.test", "role": "staff"}).json()["id"]

    r = client.post(f"/api/invite/{invite}/revoke")
    assert r.status_code == 200 and r.json()["email"] == "gone@people.test"
    assert client.get("/api/users").json()["invites"] == []

    assert client.post("/api/invite", json={
        "email": "gone@people.test", "role": "manager"}).status_code == 200


def test_withdrawing_the_same_invite_twice_is_refused_in_words(api):
    client, _org, _loc = api
    invite = client.post("/api/invite", json={
        "email": "twice@people.test", "role": "staff"}).json()["id"]
    assert client.post(f"/api/invite/{invite}/revoke").status_code == 200
    r = client.post(f"/api/invite/{invite}/revoke")
    assert r.status_code == 404
    assert "not open" in r.json()["error"]


def test_withdrawing_an_invite_that_does_not_exist_is_a_404(api):
    client, _org, _loc = api
    r = client.post(f"/api/invite/{uuid.uuid4()}/revoke")
    assert r.status_code == 404


# ------------------------------------------------------- memberships


def test_an_owner_cannot_change_their_own_role_through_the_api(api):
    """Refused here with a reason, as well as by the policy.

    The only owner of an organisation demoting themselves loses it
    permanently, with nobody left who can let them back in. The policy
    refuses it; this says why.
    """
    client, _org, _loc = api
    r = client.post(f"/api/users/{OWNER}/role", json={"role": "staff"})
    assert r.status_code == 400
    assert "your own role" in " ".join(r.json()["problems"])

    assert client.get("/api/users").json()["members"][0]["role"] == "owner"


def test_an_owner_cannot_remove_themselves(api):
    client, _org, _loc = api
    r = client.request("DELETE", f"/api/users/{OWNER}")
    assert r.status_code == 400
    assert "yourself" in r.json()["error"]
    assert len(client.get("/api/users").json()["members"]) == 1


def test_a_member_who_signed_up_can_be_promoted_and_removed(api):
    """End to end, through the trigger: invite, sign up, change, remove.

    The membership is written by the trigger on sign-up, so this is
    also the only test that proves the API's view of a second member
    matches what the schema actually created.
    """
    client, _org, loc = api
    client.post("/api/invite", json={"email": "hire@people.test",
                                     "role": "staff", "locations": [loc]})

    hire = uuid.uuid4()
    with RealDatabase(DSN, OWNER_EMAIL).as_user(OWNER) as conn:
        conn.execute("reset role")
        conn.execute("insert into auth.users (id, email) values (%s,%s)",
                     (hire, "hire@people.test"))

    listed = client.get("/api/users").json()
    assert listed["invites"] == [], "the invite was not consumed"
    mine = [m for m in listed["members"] if m["user_id"] == str(hire)]
    assert mine and mine[0]["role"] == "staff"
    assert mine[0]["locations"] == [loc]
    assert mine[0]["is_you"] is False

    r = client.post(f"/api/users/{hire}/role", json={"role": "manager"})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["role"] == "manager"

    r = client.request("DELETE", f"/api/users/{hire}")
    assert r.status_code == 200 and r.json()["was"] == "manager"
    assert [m["user_id"] for m in
            client.get("/api/users").json()["members"]] == [str(OWNER)]


def test_changing_nothing_is_refused_rather_than_reported_as_done(api):
    client, _org, _loc = api
    r = client.post(f"/api/users/{uuid.uuid4()}/role", json={})
    assert r.status_code == 400
    assert "Nothing to change" in " ".join(r.json()["problems"])


def test_an_unknown_role_is_refused_before_the_database_sees_it(api):
    client, _org, _loc = api
    r = client.post(f"/api/users/{uuid.uuid4()}/role",
                    json={"role": "superuser"})
    assert r.status_code == 400
    assert "Role has to be" in " ".join(r.json()["problems"])
