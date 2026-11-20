"""Guards on the migrations themselves. No database required.

test_rls.py proves the policies behave correctly, but it only runs where
a Postgres is reachable. These run everywhere, including on a laptop with
nothing installed and in CI before any service is up, and they catch the
mistakes that are invisible in review:

  - a new table added without RLS, which is open to the whole internet
    the moment PostgREST sees it
  - a table with RLS on and no policy, which is the opposite failure and
    looks identical in a schema diagram
  - a view without security_invoker, which runs as its owner and serves
    every tenant's rows to any caller
  - a `for all` write policy, whose USING clause also governs SELECT and
    so silently re-opens read access past the module and branch checks

Each of these has already happened once in this schema. They are cheap to
make, invisible to read, and expensive to discover in production.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))

CREATE_TABLE = re.compile(r"create table (?:if not exists )?public\.(\w+)", re.I)
CREATE_VIEW = re.compile(r"create (?:or replace )?view public\.(\w+)(.*?)as", re.I | re.S)
ENABLE_RLS = re.compile(r"alter table public\.(\w+)\s+enable row level security", re.I)
POLICY = re.compile(
    r"create policy (\w+) on public\.(\w+) for (all|select|insert|update|delete)", re.I
)


@pytest.fixture(scope="module")
def sql():
    assert MIGRATIONS, "no migrations found"
    return "\n".join(p.read_text() for p in MIGRATIONS)


@pytest.fixture(scope="module")
def tables(sql):
    return set(CREATE_TABLE.findall(sql))


def test_there_are_migrations_to_check(tables):
    assert len(tables) >= 10, f"only found {tables}"


def test_every_table_has_row_level_security_enabled(sql, tables):
    enabled = set(ENABLE_RLS.findall(sql))
    missing = sorted(tables - enabled)
    assert not missing, (
        "these tables have no RLS and are readable by anyone PostgREST "
        f"will talk to: {missing}"
    )


def test_every_table_has_at_least_one_policy(sql, tables):
    """RLS on with no policy denies everyone, which is its own outage."""
    with_policy = {t for _, t, _ in POLICY.findall(sql)}
    missing = sorted(tables - with_policy)
    assert not missing, (
        f"these tables have RLS enabled but no policy, so nobody can read "
        f"or write them at all: {missing}"
    )


def test_every_table_can_be_read_by_somebody(sql, tables):
    readable = {
        t for _, t, cmd in POLICY.findall(sql) if cmd.lower() in ("select", "all")
    }
    missing = sorted(tables - readable)
    assert not missing, f"these tables have no SELECT policy: {missing}"


def test_no_write_policy_uses_for_all(sql):
    """A FOR ALL policy's USING clause also governs SELECT.

    Permissive policies are OR'd, so a single `for all using
    (has_role(...))` re-opens read access to anyone with the role,
    straight past the module gate and the branch scoping in the read
    policy above it. This exact mistake was in the first draft of every
    table in 0002 and 0004.
    """
    offenders = [name for name, _, cmd in POLICY.findall(sql) if cmd.lower() == "all"]
    assert not offenders, (
        f"these policies use `for all`, which also grants SELECT: {offenders}. "
        f"Split them into insert / update / delete."
    )


def test_every_view_sets_security_invoker(sql):
    """Without it a view runs with its owner's rights and ignores RLS."""
    bad = [
        name for name, options, in CREATE_VIEW.findall(sql)
        if "security_invoker" not in options.lower()
    ]
    assert not bad, (
        f"these views do not set security_invoker and will serve every "
        f"tenant's rows to any caller: {bad}"
    )


def test_every_tenant_table_carries_org_id(sql, tables):
    """The wall is the same column everywhere, or it is not a wall.

    A table without org_id cannot be filtered by tenant, so either it is
    genuinely global or somebody forgot. There are no global tables in
    this schema yet, so the list of exceptions is empty on purpose.
    """
    blocks = re.split(r"(?=create table )", sql)
    missing = []
    for block in blocks:
        m = CREATE_TABLE.search(block)
        if not m:
            continue
        name = m.group(1)
        body = block[: block.index(");")] if ");" in block else block
        if name == "organisations":
            continue  # it IS the tenant
        if not re.search(r"\borg_id\b", body):
            missing.append(name)
    assert not missing, f"these tables have no org_id to filter on: {missing}"


def test_every_security_definer_function_pins_its_search_path(sql):
    """An unpinned search_path on a definer function is a privilege hole.

    The caller can create an object that shadows one the function names
    and have it run with the definer's rights.
    """
    bad = []
    for m in re.finditer(
        r"create or replace function (app\.\w+|\w+)\(.*?\$\$", sql, re.S
    ):
        body = m.group(0)
        if "security definer" in body.lower() and "set search_path" not in body.lower():
            bad.append(m.group(1))
    assert not bad, f"these SECURITY DEFINER functions do not pin search_path: {bad}"


def test_helpers_are_not_in_an_exposed_schema(sql):
    """PostgREST exposes `public`.

    A SECURITY DEFINER helper there can be called straight through the
    API, which hands a caller the very bypass the helper exists to
    contain. They belong in `app`, which is not exposed.
    """
    for m in re.finditer(r"create or replace function (\S+?)\(.*?\$\$", sql, re.S):
        if "security definer" in m.group(0).lower():
            assert m.group(1).startswith("app."), (
                f"{m.group(1)} is SECURITY DEFINER but not in the app schema"
            )


def test_migrations_are_numbered_without_gaps_or_duplicates(sql):
    """Applied in filename order, so the order has to be unambiguous."""
    numbers = [int(p.name.split("_")[0]) for p in MIGRATIONS]
    assert numbers == sorted(numbers)
    assert len(numbers) == len(set(numbers)), f"duplicate migration numbers: {numbers}"
    assert numbers == list(range(1, len(numbers) + 1)), f"gap in numbering: {numbers}"


def test_the_ledger_is_append_only(sql):
    """Enforced by trigger, not only by the absence of a policy.

    A missing policy stops a signed-in user. It does nothing about the
    service role, which bypasses RLS entirely and is what a background
    job runs as.
    """
    assert "before update or delete on public.stock_movement" in sql.lower(), (
        "stock_movement has no trigger guarding against update or delete"
    )
    updates = [
        name for name, table, cmd in POLICY.findall(sql)
        if table == "stock_movement" and cmd.lower() in ("update", "delete", "all")
    ]
    assert not updates, (
        f"stock_movement has an update or delete policy ({updates}), which "
        f"contradicts an append-only ledger"
    )
