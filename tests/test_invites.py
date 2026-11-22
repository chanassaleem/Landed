"""Invites, against a real Postgres.

An organisation has had exactly one member since migration 0001:
whoever created it. `memberships` carried a role, a branch scope and an
`invited_by` column the whole time with no way to write a second row, so
"invite a colleague" was unreachable rather than unbuilt.

The mechanism is a trigger on `auth.users` that runs as the definer,
and that is the part worth testing hard. It has to run as the definer
because the person signing up has no membership yet and so cannot see
their own invite under the policies -- the invite is what grants the
access it would be read with. A SECURITY DEFINER function that reads a
table keyed on an attacker-supplied value is exactly the shape of
mistake that hands someone else's organisation over, so each guard here
is tested by making it fail, not by reading the SQL.

They run wherever a Postgres is reachable, and skip when one is not:

    source scripts/dev_postgres.sh
    python3 -m pytest tests/test_invites.py
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("LANDED_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="LANDED_TEST_DSN not set")

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
SHIM = ROOT / "supabase" / "tests" / "00_supabase_shim.sql"


@pytest.fixture(scope="module")
def db():
    con = psycopg.connect(DSN, autocommit=True)
    con.execute("drop schema if exists public cascade")
    con.execute("drop schema if exists app cascade")
    con.execute("drop schema if exists auth cascade")
    con.execute("create schema public")
    for path in [SHIM, *MIGRATIONS]:
        con.execute(path.read_text())
    yield con
    con.close()


@contextlib.contextmanager
def as_user(con, user_id, email=None):
    """Act as that signed-in user for the length of the block.

    The email goes into the claims because a real Supabase JWT carries
    one and the invite read policy matches on it. A harness that omitted
    it would make that policy look like it denied everything.
    """
    with con.transaction():
        con.execute("set local role authenticated")
        claims = {"sub": str(user_id), "role": "authenticated"}
        if email is not None:
            claims["email"] = email
        con.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (json.dumps(claims),),
        )
        yield con


def new_user(con, email):
    uid = uuid.uuid4()
    con.execute("insert into auth.users (id, email) values (%s, %s)", (uid, email))
    return uid


def new_org(con, owner, name):
    with as_user(con, owner):
        org = con.execute(
            "insert into public.organisations (name, created_by)"
            " values (%s, %s) returning id",
            (name, owner),
        ).fetchone()[0]
        loc = con.execute(
            "insert into public.locations (org_id, code, name)"
            " values (%s, 'MAIN', 'Main') returning id",
            (org,),
        ).fetchone()[0]
    return org, loc


def invite(con, org, owner, email, role="staff", locations=None, **cols):
    """Write an invite as the owner, optionally overriding a column.

    The overrides exist so a test can age or revoke an invite without a
    separate update statement, which would itself have to pass a policy
    and would muddle what is being tested.
    """
    sets, vals = [], []
    for k, v in cols.items():
        sets.append(k)
        vals.append(v)
    names = ", ".join(["org_id", "email", "role", "location_ids", "invited_by", *sets])
    marks = ", ".join(["%s"] * (5 + len(sets)))
    with as_user(con, owner, "owner@a.test") as c:
        return c.execute(
            f"insert into public.invites ({names}) values ({marks}) returning id",
            (org, email, role, locations or [], owner, *vals),
        ).fetchone()[0]


def members(con, org):
    return {
        r[0]: (r[1], r[2])
        for r in con.execute(
            "select user_id, role, location_ids from public.memberships"
            " where org_id = %s",
            (org,),
        ).fetchall()
    }


# ------------------------------------------------------------ accepting


def test_signing_up_accepts_a_live_invite(db):
    owner = new_user(db, "owner@a.test")
    org, loc = new_org(db, owner, "Importer A")
    invite(db, org, owner, "newbie@a.test", role="manager", locations=[loc])

    assert len(members(db, org)) == 1

    newbie = new_user(db, "newbie@a.test")
    got = members(db, org)
    assert newbie in got, "signing up did not grant the invited membership"
    assert got[newbie] == ("manager", [loc]), \
        "the role or the branch scope was not carried across"


def test_the_invite_is_marked_accepted_by_whoever_accepted_it(db):
    owner = new_user(db, "owner2@a.test")
    org, _ = new_org(db, owner, "Importer A2")
    inv = invite(db, org, owner, "second@a.test")

    user = new_user(db, "second@a.test")
    at, by = db.execute(
        "select accepted_at, accepted_by from public.invites where id = %s",
        (inv,),
    ).fetchone()
    assert at is not None
    assert by == user


def test_an_address_is_matched_whatever_its_case(db):
    """GoTrue does not fold the address, so the schema does.

    Without this an invite to `Ahmed@x.test` is dead on arrival: the
    person signs up, nothing happens, and there is no error anywhere to
    explain why they have a login and no organisation.
    """
    owner = new_user(db, "owner3@a.test")
    org, _ = new_org(db, owner, "Importer A3")
    invite(db, org, owner, "mixed@a.test")

    user = new_user(db, "MiXeD@a.test")
    assert user in members(db, org)


def test_an_invite_may_not_be_stored_unfolded(db):
    """The fold is a constraint, not a convention in the API.

    Everything downstream compares lowered addresses, so an unfolded row
    would be invisible to the trigger and to the uniqueness index at
    once. Refusing it at the table means there is one way to be wrong
    and it fails loudly.
    """
    owner = new_user(db, "owner4@a.test")
    org, _ = new_org(db, owner, "Importer A4")
    with pytest.raises(psycopg.errors.CheckViolation):
        invite(db, org, owner, "NotFolded@a.test")


# -------------------------------------------------------------- guards


def test_an_expired_invite_grants_nothing(db):
    """The guard that matters most.

    An invite that never expires is a permanent key to an organisation
    sitting in a table, and the person who sent it has forgotten it
    exists. Tested by making it fail: remove `expires_at > now()` from
    app.accept_invites and this is the test that goes red.
    """
    owner = new_user(db, "owner5@a.test")
    org, _ = new_org(db, owner, "Importer A5")
    invite(db, org, owner, "stale@a.test",
           expires_at="2020-01-01T00:00:00Z")

    user = new_user(db, "stale@a.test")
    assert user not in members(db, org), "an expired invite was accepted"


def test_a_revoked_invite_grants_nothing(db):
    owner = new_user(db, "owner6@a.test")
    org, _ = new_org(db, owner, "Importer A6")
    invite(db, org, owner, "revoked@a.test",
           revoked_at="2024-01-01T00:00:00Z")

    user = new_user(db, "revoked@a.test")
    assert user not in members(db, org), "a revoked invite was accepted"


def test_an_invite_is_not_accepted_twice(db):
    """A second account on the same address must not inherit the seat.

    Addresses get recycled, and an accepted invite that stays live is a
    seat anyone who later owns that mailbox can claim.
    """
    owner = new_user(db, "owner7@a.test")
    org, _ = new_org(db, owner, "Importer A7")
    invite(db, org, owner, "once@a.test")

    first = new_user(db, "once@a.test")
    assert first in members(db, org)

    db.execute("delete from auth.users where id = %s", (first,))
    second = new_user(db, "once@a.test")
    assert second not in members(db, org), \
        "an already-accepted invite was accepted a second time"


def test_only_one_live_invite_per_address_per_organisation(db):
    owner = new_user(db, "owner8@a.test")
    org, _ = new_org(db, owner, "Importer A8")
    invite(db, org, owner, "dup@a.test")
    with pytest.raises(psycopg.errors.UniqueViolation):
        invite(db, org, owner, "dup@a.test", role="manager")


def test_a_lapsed_invite_can_be_reissued(db):
    """The other half of the uniqueness rule.

    Scoping the index to live invites is what makes this work. A plain
    unique constraint would make a revoked invite permanent, and the
    only way back would be a support request.
    """
    owner = new_user(db, "owner9@a.test")
    org, _ = new_org(db, owner, "Importer A9")
    invite(db, org, owner, "again@a.test",
           revoked_at="2024-01-01T00:00:00Z")
    assert invite(db, org, owner, "again@a.test", role="manager") is not None


# ------------------------------------------------------------- tenancy


def test_an_invite_reaches_only_the_organisation_it_names(db):
    """Two organisations, the same invited address, one membership.

    The trigger reads every live invite for the address, so an invite
    into B must not pull the person into A as well.
    """
    a_owner = new_user(db, "a-owner@a.test")
    b_owner = new_user(db, "b-owner@b.test")
    org_a, _ = new_org(db, a_owner, "Importer A10")
    org_b, _ = new_org(db, b_owner, "Importer B10")

    with as_user(db, b_owner, "b-owner@b.test") as c:
        c.execute(
            "insert into public.invites (org_id, email, role, invited_by)"
            " values (%s, 'shared@x.test', 'staff', %s)",
            (org_b, b_owner),
        )

    user = new_user(db, "shared@x.test")
    assert user in members(db, org_b)
    assert user not in members(db, org_a), \
        "an invite into one organisation granted access to another"


def test_a_stranger_cannot_list_invites(db):
    owner = new_user(db, "owner11@a.test")
    org, _ = new_org(db, owner, "Importer A11")
    invite(db, org, owner, "target@a.test")

    stranger = new_user(db, "stranger@z.test")
    with as_user(db, stranger, "stranger@z.test") as c:
        assert c.execute(
            "select count(*) from public.invites").fetchone()[0] == 0


def test_the_invited_person_can_see_their_own_invite(db):
    """Before they have a membership, matched on the email in the JWT.

    They need this to be told which organisation is asking them in.
    Keyed on the claim rather than on the row means nobody can list
    invites addressed to anyone else.
    """
    owner = new_user(db, "owner12@a.test")
    org, _ = new_org(db, owner, "Importer A12")
    invite(db, org, owner, "pending@a.test")

    nobody = new_user(db, "unrelated@z.test")
    with as_user(db, nobody, "pending@a.test") as c:
        rows = c.execute(
            "select org_id from public.invites").fetchall()
    assert [r[0] for r in rows] == [org]


def test_staff_cannot_invite_anyone(db):
    owner = new_user(db, "owner13@a.test")
    org, loc = new_org(db, owner, "Importer A13")
    invite(db, org, owner, "clerk@a.test", role="staff", locations=[loc])
    clerk = new_user(db, "clerk@a.test")

    with as_user(db, clerk, "clerk@a.test") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                "insert into public.invites (org_id, email, role, invited_by)"
                " values (%s, 'friend@a.test', 'owner', %s)",
                (org, clerk),
            )


def test_an_invite_cannot_be_attributed_to_someone_else(db):
    """`invited_by` has to be the person making the request.

    Otherwise a manager can write an invite that reads as the owner's,
    and the audit trail of who let someone in is worthless.
    """
    owner = new_user(db, "owner14@a.test")
    org, loc = new_org(db, owner, "Importer A14")
    invite(db, org, owner, "boss@a.test", role="manager", locations=[loc])
    manager = new_user(db, "boss@a.test")

    with as_user(db, manager, "boss@a.test") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                "insert into public.invites (org_id, email, role, invited_by)"
                " values (%s, 'ghost@a.test', 'staff', %s)",
                (org, owner),
            )


# --------------------------------------------------------- memberships


def test_an_owner_cannot_lock_themselves_out(db):
    """Migration 0009 replaces 0001's rule for exactly this.

    As written, an owner could delete their own membership. The only
    owner of an organisation doing that loses it permanently, with
    nobody left who can let them back in.
    """
    owner = new_user(db, "owner15@a.test")
    org, _ = new_org(db, owner, "Importer A15")

    with as_user(db, owner, "owner15@a.test") as c:
        c.execute(
            "delete from public.memberships where org_id = %s and user_id = %s",
            (org, owner),
        )
    assert owner in members(db, org)

    with as_user(db, owner, "owner15@a.test") as c:
        c.execute(
            "update public.memberships set role = 'staff'"
            " where org_id = %s and user_id = %s",
            (org, owner),
        )
    assert members(db, org)[owner][0] == "owner", \
        "an owner demoted themselves to staff"


def test_an_owner_may_change_somebody_else(db):
    owner = new_user(db, "owner16@a.test")
    org, loc = new_org(db, owner, "Importer A16")
    invite(db, org, owner, "hire@a.test", role="staff", locations=[loc])
    hire = new_user(db, "hire@a.test")

    with as_user(db, owner, "owner16@a.test") as c:
        c.execute(
            "update public.memberships set role = 'manager'"
            " where org_id = %s and user_id = %s",
            (org, hire),
        )
    assert members(db, org)[hire][0] == "manager"


def test_a_manager_cannot_change_a_membership(db):
    owner = new_user(db, "owner17@a.test")
    org, loc = new_org(db, owner, "Importer A17")
    invite(db, org, owner, "mgr@a.test", role="manager", locations=[loc])
    mgr = new_user(db, "mgr@a.test")
    invite(db, org, owner, "junior@a.test", role="staff", locations=[loc])
    junior = new_user(db, "junior@a.test")

    with as_user(db, mgr, "mgr@a.test") as c:
        c.execute(
            "update public.memberships set role = 'owner'"
            " where org_id = %s and user_id = %s",
            (org, junior),
        )
    assert members(db, org)[junior][0] == "staff", \
        "a manager promoted somebody"
