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


def test_nothing_paints_text_near_black_in_both_themes():
    """--ink is a fixed near-black that does not flip with the theme.
    Used as a text colour it is invisible on a dark background, which
    is how the business's own name in the header came to be a shade
    away from the header behind it. The file documents this trap forty
    lines in, for the money band, and it was walked into anyway.

    The one legitimate use is a surface that deliberately inverts --
    the tooltip, which paints itself light in the dark theme and needs
    a near-black on top of it. So: inside a [data-theme='dark'] block
    it is fine, and outside one it is not.
    """
    plain = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    offenders = []
    # (?<![-\w]) so `border-color` does not match: it ends in the same
    # five letters, and without the guard this accused three rules
    # that only set a border.
    for match in re.finditer(r"(?<![-\w])color:\s*var\(--ink\)", plain):
        brace = plain.rindex("{", 0, match.start())
        cut = max(plain.rfind("}", 0, brace), plain.rfind("{", 0, brace))
        selector = plain[cut + 1:brace].strip()
        if "[data-theme='dark']" not in selector:
            offenders.append(selector.splitlines()[-1].strip()[:60])
    assert not offenders, (
        f"near-black text outside a deliberately inverted surface: "
        f"{offenders}")


def _contrast(a: str, b: str) -> float:
    def lin(c):
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def lum(h):
        h = h.lstrip("#")
        r, g, bl = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(bl)

    hi, lo = sorted([lum(a), lum(b)], reverse=True)
    return round((hi + 0.05) / (lo + 0.05), 2)


def _theme_tokens(start: str, end: str | None) -> dict:
    """The hex tokens one theme declares.

    The dark block ends at its own closing brace; the light block runs
    to where the dark one begins, because a comment inside it contains
    a brace and slicing on the first "\n}" cuts it short -- which is
    how the first version of this read an empty dictionary and
    reported no colours at all rather than the two that were failing.
    """
    i = CSS.index(start)
    j = CSS.index(end, i) if end else CSS.index("\n}", i)
    got = {name.lstrip("-"): value for name, value in re.findall(
        r"(--[a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", CSS[i:j])}
    assert "surface" in got and "text" in got, (
        f"no tokens found after {start!r}; the block boundaries moved")
    return got


def test_every_text_colour_meets_aa_on_every_surface_it_sits_on():
    """Measured, not eyeballed. --text-3 was 3.1:1 in the light theme
    and 3.8:1 in the dark, against the 4.5:1 WCAG 2.2 AA asks of
    normal text -- and it carries the metadata under every figure on
    every screen. A grey that looks quiet and a grey that cannot be
    read are the same grey to anyone designing in good light on a good
    display."""
    themes = {
        "light": _theme_tokens(":root {", "[data-theme="),
        "dark": _theme_tokens("[data-theme='dark'] {", None),
    }
    failures = []
    for name, t in themes.items():
        backgrounds = [t[k] for k in ("surface", "paper", "sunk") if k in t]
        for tok in ("text", "text-2", "text-3", "attention", "urgent",
                    "transit", "idle"):
            if tok not in t:
                continue
            for bg in backgrounds:
                got = _contrast(t[tok], bg)
                if got < 4.5:
                    failures.append(f"{name} --{tok} {t[tok]} on {bg}: {got}:1")
    assert not failures, "below WCAG AA 4.5:1 — " + "; ".join(failures)


def test_the_three_text_weights_are_still_told_apart():
    """Making the quietest one readable must not make it the same as
    the one above it, or the hierarchy the screens rely on collapses."""
    for name, start, end in (("light", ":root {", "[data-theme="),
                             ("dark", "[data-theme='dark'] {", None)):
        t = _theme_tokens(start, end)
        surface = t["surface"]
        got = [_contrast(t[k], surface) for k in ("text", "text-2", "text-3")]
        assert got[0] > got[1] > got[2], f"{name}: {got}"
        assert got[1] - got[2] >= 0.8, (
            f"{name}: --text-2 and --text-3 are {got[1]} and {got[2]}, "
            f"too close to read as different weights")


# -------------------------------------- a provisional figure says so
#
# Migration 0024 flags a landed cost whose shipment has a charge that
# could not be divided properly. The flag is worth nothing if the
# screen prints the figure to the same two decimal places as a settled
# one, so these pin the marking rather than the colour.

def _platform_js() -> str:
    return (pathlib.Path(__file__).resolve().parents[1]
            / "web" / "static" / "platform.js").read_text()


def test_the_stock_screen_marks_every_figure_built_on_a_provisional_cost():
    """The cost and both margins, because a margin computed off an
    unsettled cost is exactly as unsettled -- and the margin is the
    figure somebody actually prices against."""
    js = _platform_js()
    i = js.index("    stock: {")
    block = js[i:js.index("cols: [", i)]
    assert "mark:" in block, "the stock module no longer marks anything"
    for col in ("unit_cost", "margin", "margin_pct"):
        assert f"'{col}'" in block, f"{col} is no longer marked"


def test_the_mark_is_not_carried_by_colour_alone():
    """A printout, a projector and a reader who cannot separate these
    hues all lose a colour-only signal."""
    grid = (pathlib.Path(__file__).resolve().parents[1]
            / "web" / "static" / "grid.js").read_text()
    # The whole branch, not a fixed slice of characters after the
    # marker: a slice breaks when somebody adds a comment inside it,
    # which says nothing about whether the behaviour is still there.
    start = grid.index("const why = mark ? mark(r, c) : null;")
    depth, i = 0, grid.index("if (why) {", start)
    j = i
    while True:
        if grid[j] == "{":
            depth += 1
        elif grid[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    branch = grid[i:j]
    assert "unsure-mark" in branch, "no glyph beside the figure"
    assert "aria-label" in branch, "nothing for a screen reader"


def test_the_provisional_colour_is_one_of_the_four():
    """The stylesheet says the four state colours are the only
    chromatic values in the product. A fifth hue for this would start
    the drift that ends with a palette nobody can reason about."""
    css = CSS[CSS.index("td.unsure {"):]
    rule = css[:css.index("}")]
    assert "var(--attention)" in rule, (
        "td.unsure should use --attention, the decide-this-week value, "
        f"not a new colour: {rule!r}")
