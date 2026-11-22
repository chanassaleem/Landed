"""The module registry, and the guards that keep it honest.

Before engine/platform.py the module list lived in three places that
disagreed: three modules in the browser, twelve differently-named ones
in the old auth map, eleven in a check constraint. These tests exist so
that cannot come back -- a module declared in the registry with no
runner, a runner with no module, or an engine number the product cannot
reach are each a failing test rather than something found later by
somebody wondering why a screen is blank.
"""

from __future__ import annotations

import dataclasses
import enum
from datetime import date

import pytest

from engine import platform
from engine.schema import Thresholds
from web import modules as mod
from web.app import SETTABLE


# -------------------------------------------------------- the registry


def test_every_module_has_a_runner():
    assert set(mod.RUNNERS) == {m.id for m in platform.MODULES}


def test_every_module_id_is_unique():
    ids = [m.id for m in platform.MODULES]
    assert len(ids) == len(set(ids))


def test_every_module_sits_in_a_declared_section():
    known = {i for i, _ in platform.SECTIONS}
    assert {m.section for m in platform.MODULES} <= known


def test_every_declared_need_is_a_known_record_type():
    """A typo in `needs` would make a module permanently `waiting`.

    It would look like missing data rather than a missing record type,
    and the person would go off to fill in a table that is already
    full.
    """
    for m in platform.MODULES:
        for r in (*m.needs, *m.uses):
            assert r in platform.RECORDS, f"{m.id} needs unknown {r!r}"


def test_every_engine_reference_resolves():
    """`engine` names a real function, or is None on purpose.

    Four modules keep records rather than computing over them, and the
    registry says so with None. A string that does not resolve is the
    other case: a module claiming arithmetic that is not there.
    """
    import importlib

    for m in platform.MODULES:
        if m.engine is None:
            continue
        where, _, func = m.engine.rpartition(".")
        for package in ("engine.modules", "engine"):
            try:
                target = importlib.import_module(f"{package}.{where}")
            except ModuleNotFoundError:
                continue
            assert hasattr(target, func), f"{m.id}: no {m.engine}"
            break
        else:
            pytest.fail(f"{m.id}: cannot import {m.engine}")


def test_the_record_keepers_declare_no_engine():
    """The four without arithmetic say so, rather than naming a module
    invented to hold a SELECT."""
    assert {m.id for m in platform.MODULES if m.engine is None} == {
        "freight", "sell", "customers"}


# ------------------------------------------------------------- states


def test_a_module_with_its_records_is_ready():
    got = {m["id"]: m for m in platform.catalogue(present={"items"})}
    assert got["stock"]["state"] == "ready"


def test_a_module_without_its_records_is_waiting_and_says_which():
    got = {m["id"]: m for m in platform.catalogue(present=set())}
    assert got["demand"]["state"] == "waiting"
    assert set(got["demand"]["missing"]) == {"items", "demand"}


def test_what_a_module_merely_uses_does_not_hold_it_back():
    """The cash view works from invoices and is sharper with
    commitments. Reporting that as a hard dependency would hide a
    working module behind a table nobody has filled in."""
    got = {m["id"]: m for m in platform.catalogue(present={"invoices"})}
    assert got["cash"]["state"] == "ready"
    assert "commitments" in got["cash"]["uses"]


def test_an_unlicensed_module_is_locked_not_missing():
    got = {m["id"]: m
           for m in platform.catalogue(present={"items"}, licensed={"buy"})}
    assert got["stock"]["state"] == "locked"
    assert got["buy"]["state"] == "ready"
    # Licensed but with nothing to read is still "waiting", not
    # "locked": the two have different remedies and conflating them
    # sends somebody to a sales conversation about a table they could
    # fill in themselves.
    thin = {m["id"]: m
            for m in platform.catalogue(present=set(), licensed={"buy"})}
    assert thin["buy"]["state"] == "waiting"


def test_a_module_the_role_may_not_open_is_absent_entirely():
    """Not locked, not hidden-but-listed. Absent.

    A rail entry that says "the books, which you may not see" tells
    somebody there are books, how many accounts there are when they
    hover, and that this company keeps them. The role gate is a
    permission, so nothing about the module reaches the browser.
    """
    got = {m["id"] for m in platform.catalogue(present={"accounts"},
                                               role="manager")}
    assert "books" not in got
    assert "people" not in got
    assert "stock" in got


def test_an_unknown_role_gets_nothing():
    """A typo in a membership row locks someone out visibly.

    The alternative -- an unrecognised role falling through to every
    module -- hands them the payroll.
    """
    assert platform.for_role("administrator") == ()
    assert platform.catalogue(present={"items"}, role="administrator") == []


def test_staff_cannot_reach_the_money_modules():
    got = {m.id for m in platform.for_role("staff")}
    assert "books" not in got and "people" not in got
    assert "cash" not in got and "money" not in got
    assert "stock" in got and "service" in got


def test_an_accountant_cannot_reach_the_pipeline():
    got = {m.id for m in platform.for_role("accountant")}
    assert "intelligence" not in got
    assert "books" in got and "money" in got


def test_get_names_the_module_it_cannot_find():
    with pytest.raises(KeyError, match="nonsense"):
        platform.get("nonsense")


# ------------------------------------------------------- settings drift


def test_every_number_the_engine_reads_is_settable():
    """The whole point of Thresholds is that nothing is hard-coded.

    A field in the dataclass that the product cannot set is hard-coded
    in practice: the person has no way to reach it, and the only
    evidence of its existence is a figure they disagree with. Nineteen
    of these were in exactly that state.
    """
    numeric = {
        f.name for f in dataclasses.fields(Thresholds)
        if isinstance(getattr(Thresholds(), f.name), (int, float))
        and not isinstance(getattr(Thresholds(), f.name), bool)
    }
    missing = numeric - set(SETTABLE)
    assert not missing, f"settable in the engine, unreachable here: {missing}"


def test_no_setting_refers_to_a_field_the_engine_does_not_have():
    known = {f.name for f in dataclasses.fields(Thresholds)}
    assert set(SETTABLE) <= known


def test_the_integer_settings_stay_integers():
    """Thresholds holds ints for the fields that index or iterate.

    cash_horizon_weeks reaches a range() and target_dso_days a
    timedelta, so a float arriving there is a TypeError deep inside a
    module rather than a rejection at the edge. The old table held only
    floats, so this would have broken the first time a week count was
    exposed.
    """
    from web.app import thresholds_for

    t = thresholds_for({"cash_horizon_weeks": 20.0, "target_dso_days": 45.9,
                        "forecast_horizon_months": 9})
    assert isinstance(t.cash_horizon_weeks, int) and t.cash_horizon_weeks == 20
    assert isinstance(t.target_dso_days, int)
    assert isinstance(t.forecast_horizon_months, int)


def test_every_setting_belongs_to_a_module_that_exists():
    for key, spec in SETTABLE.items():
        if not spec.module:
            continue
        assert spec.module in platform.BY_ID, f"{key} -> {spec.module}"


def test_a_setting_out_of_range_is_refused_not_clamped():
    """Clamping is the software deciding what the business meant.

    Somebody typing 400% for the cost of capital has made a mistake
    worth being told about; storing 100% quietly hides it until a
    figure they cannot explain turns up.
    """
    value, why = SETTABLE["cost_of_capital_annual"].clean(4.0)
    assert value is None and "between" in why


def test_the_plain_settings_are_the_ones_without_arithmetic_behind_them():
    """Eight of twenty-three are on the first screen.

    The split is by whether a person can answer the question without
    being told how a module works. "What does money cost you a year" is
    answerable; "level smoothing" is not.
    """
    plain = {k for k, s in SETTABLE.items() if not s.advanced}
    assert "holt_alpha" not in plain
    assert "croston_alpha" not in plain
    assert "target_cover_months" in plain
    assert "cost_of_capital_annual" in plain


# --------------------------------------------------------- serialising


def test_jsonable_unwraps_a_dataclass_of_enums_and_dates():
    from engine.schema import StockStatus

    @dataclasses.dataclass(frozen=True)
    class Row:
        when: date
        status: StockStatus
        qty: float

    got = mod.jsonable(Row(date(2026, 3, 1), StockStatus.CRITICAL, 1.23456789))
    assert got == {"when": "2026-03-01",
                   "status": StockStatus.CRITICAL.value, "qty": 1.2346}
    # The enum's value, whatever its spelling -- asserting the literal
    # would make this a test of how StockStatus is spelt rather than of
    # whether the enum survives serialising at all.
    assert isinstance(got["status"], str)


def test_jsonable_flattens_tuple_keys():
    """The service-target table is keyed on (abc class, demand class),
    and JSON has no tuple keys. Dropping them silently would lose the
    whole table."""
    got = mod.jsonable({("A", "smooth"): 0.98})
    assert got == {"A|smooth": 0.98}


def test_jsonable_keeps_none_distinct_from_zero():
    """An accuracy figure measured on a holdout is None when there was
    too little history to hold any back. Turning that into zero reports
    a perfectly wrong forecast as a perfectly bad one."""
    assert mod.jsonable({"mape": None, "zero": 0.0}) == {
        "mape": None, "zero": 0.0}


def test_jsonable_handles_an_unprintable_value_without_raising():
    class Odd:
        def __repr__(self): return "<odd>"

    assert isinstance(mod.jsonable(Odd()), str)
