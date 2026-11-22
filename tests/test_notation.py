"""One number, one notation.

A Pakistani screen was showing 66.0L in a column and PKR 6.6M in the
sentence beside it. They are the same amount and a reader has to stop
and work out that they are, which is the precise failure
engine/country.py's own docstring warns about: "a figure shown the
wrong way is not slightly awkward, it is unreadable".

The cause was four private copies of one formatter, each with the
scale hard-coded to millions. These tests are about the copies as much
as the output -- a fifth would bring the bug back.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from engine.fmt import money
from engine.schema import Thresholds

ENGINE = pathlib.Path(__file__).resolve().parents[1] / "engine"


# ------------------------------------------------------- the formatter


@pytest.mark.parametrize("amount,western,indian", [
    (6_612_480, "PKR 6.6M", "PKR 66.1L"),
    (3_100_000, "PKR 3.1M", "PKR 31.0L"),
    (11_000_000, "PKR 11.0M", "PKR 1.1cr"),
    (57_000, "PKR 57K", "PKR 57k"),
    (940, "PKR 940", "PKR 940"),
    (0, "PKR 0", "PKR 0"),
])
def test_both_scales(amount, western, indian):
    assert money(amount) == western
    assert money(amount, scale="indian") == indian


def test_a_negative_keeps_its_sign_in_front_of_the_currency():
    assert money(-250_000, scale="indian") == "-PKR 2.5L"


def test_the_currency_is_not_assumed():
    assert money(6_612_480, "AED", "western") == "AED 6.6M"
    assert "PKR" not in money(1, "KES")


def test_an_unknown_scale_falls_back_rather_than_raising():
    """A settings value this code has not met should not take a screen
    down. Western is the safer fallback: it is wrong for one reader
    rather than unreadable to them."""
    assert money(6_612_480, "PKR", "klingon") == "PKR 6.6M"


def test_the_default_is_western_so_an_unaware_caller_does_not_switch():
    """Sixty-odd call sites pass no scale. The default must not quietly
    put lakhs in front of a business in Dubai."""
    assert money(6_612_480).endswith("M")


# --------------------------------------------------- and only the one


def test_no_module_writes_its_own_money_formatter():
    """The bug was four copies, not one mistake.

    A private `_money` with a divisor in it is how the same amount came
    to appear two ways on one screen. If a module needs money in a
    sentence it imports engine.fmt.money.
    """
    offenders = []
    for path in ENGINE.rglob("*.py"):
        if path.name == "fmt.py":
            continue
        text = path.read_text()
        for pattern in (r"/\s*1e6", r"/\s*1_000_000", r"/\s*1000000"):
            if re.search(pattern, text):
                offenders.append(f"{path.relative_to(ENGINE)}: {pattern}")
    assert not offenders, (
        "money formatted by hand instead of through engine.fmt.money: "
        + "; ".join(offenders))


def test_thresholds_carries_the_scale_so_a_module_can_reach_it():
    """The modules that write sentences receive Thresholds and nothing
    else about the organisation, so this is where the scale has to
    live for them to honour it."""
    t = Thresholds()
    assert t.currency == "PKR"
    assert t.number_scale == "indian"
    assert money(6_612_480, t.currency, t.number_scale) == "PKR 66.1L"


def test_the_settings_vocabulary_matches_the_formatter_vocabulary():
    """Settings store "indian" or "western"; a country pack reports
    Scale.grouping, which is the same two words. A third vocabulary is
    how the settings screen and the engine end up disagreeing about
    which country somebody is in."""
    from engine.country import PACKS
    from engine.fmt import SCALES

    assert set(SCALES) == {"indian", "western"}
    for code, p in PACKS.items():
        assert p.scale.grouping in SCALES, \
            f"the {code} pack groups digits a way money() cannot write"


@pytest.mark.parametrize("module", ["sequence", "machines", "cash"])
def test_an_evaluator_honours_the_organisation_scale(module):
    """Every money() inside these evaluators passes the organisation's
    currency and scale. A bare money() there is a sentence that will
    read in the wrong notation."""
    text = (ENGINE / "modules" / f"{module}.py").read_text()
    bare = re.findall(r"(?<![.\w])money\([^()]*\)(?!\s*,)", text)
    bare = [b for b in bare if "thresholds" not in b]
    assert not bare, f"{module}.py: {bare}"
