"""The layout at a phone's width.

Checked in a browser at 430px, and the findings pinned here. A CSS
assertion cannot see a rendering fault, so these encode the specific
mistakes that produced one rather than standing in for looking.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = (ROOT / "web" / "static" / "app.css").read_text()


def _block(selector_line: str) -> str:
    """The media query block containing a given line."""
    i = CSS.index(selector_line)
    start = CSS.rindex("@media", 0, i)
    return CSS[start:CSS.index("\n}", i)]


def test_the_horizontal_rail_hides_its_section_headings():
    """At 430px the rail becomes a horizontal scroller. The section
    headings -- Goods, Money, People, Service, Decide -- each collapsed
    to a 20px box with overflow hidden, so five two-letter slivers of
    clipped capitals sat wedged between the real items. It reads as a
    rendering fault, which is the worst thing to have in navigation.

    .sep, .rail-pin and .count were hidden there; .rail-section was
    missed."""
    block = _block(".rail .rail-section { display: none; }")
    assert "max-width: 860px" in block
    for hidden in (".sep", ".rail-pin", ".count", ".rail-section"):
        assert hidden in block, f"{hidden} is not hidden in the strip"


def test_that_override_is_specific_enough_to_win():
    """The real bug, and the reason the first fix did nothing.

    The base `.rail-section` rule is four hundred lines further down
    the file. A bare `.rail-section` in the media query has equal
    specificity and loses on source order: the rule applies and
    nothing happens. Scoping it under `.rail` is what makes it win.
    """
    override = re.search(r"^\s*(\.rail \.rail-section) \{ display: none; \}",
                         CSS, re.M)
    assert override, "the override is no longer scoped under .rail"
    base = CSS.index("\n.rail-section {")
    assert base > CSS.index(override.group(1)), (
        "the base rule moved above the override, so the scoping that "
        "made this work may no longer be what is carrying it")


def test_nothing_declares_a_colour_token_that_does_not_exist():
    """--line and --raise have both been written in this file when the
    real names were --rule and --sunk. A var() that does not resolve
    renders as nothing, which looks like a layout bug rather than a
    typo."""
    # Comments are stripped first and both anchors allowed. A
    # line-anchored pattern misses `:root { --content: 2100px; }`,
    # which declares one on the same line as its selector; a
    # brace-or-semicolon anchor misses the declaration that follows a
    # comment. Getting this wrong makes the test accuse the stylesheet
    # of the bug it is looking for, which is how a check like this
    # ends up deleted.
    plain = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    declared = set(re.findall(r"(?:[{;]|^)\s*(--[a-z0-9-]+)\s*:",
                              plain, re.M))
    used = set(re.findall(r"var\((--[a-z0-9-]+)[,)]", plain))
    assert used <= declared, (
        f"not declared anywhere: {sorted(used - declared)}")
