"""Naming the row that is wrong, and the screen that fixes it.

A module that finds something wrong has had two ways to say so: a total
that looks odd, and a sentence in a note. Neither names the row. "PKR
560,000 reached no item" worries somebody without telling them which of
six charges on which of five shipments, or what to do about it.

Both were driven in a browser against an imported workbook: the one
stranded charge at the top of the freight panel in full colour with its
corrective action on one line, eight "awaiting goods" charges accounted
for in a single sentence below, and a data-quality finding about
machines opening the equipment screen.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLATFORM = (ROOT / "web" / "static" / "platform.js").read_text()
CSS = (ROOT / "web" / "static" / "app.css").read_text()
SANITY = (ROOT / "engine" / "sanity.py").read_text()
MODULES = (ROOT / "web" / "modules.py").read_text()


def test_the_freight_screen_names_the_charges_that_went_nowhere():
    spec = PLATFORM[PLATFORM.index("    freight: {"):]
    spec = spec[:spec.index("\n    demand: {")]
    assert "panels: [{" in spec
    assert "key: 'problems'" in spec
    for col in ("shipment", "kind", "billed", "blocked_by", "action"):
        assert f"['{col}'," in spec, f"the panel does not show {col}"


def test_the_module_sends_an_action_for_every_reason_it_can_report():
    """The drift that matters. A new blocked_by with no action is a
    row that names a problem and offers nothing to do about it."""
    reasons = set(re.findall(r"then '([a-z ]+)'\s*$", MODULES, re.M))
    actions = MODULES[MODULES.index("    actions = {"):]
    actions = actions[:actions.index("\n    }")]
    named = set(re.findall(r'^\s{8}"([a-z ]+)":', actions, re.M))
    view = (ROOT / "supabase" / "migrations"
            / "0019_allocation_corrections.sql").read_text()
    emitted = set(re.findall(r"then '([a-z ]+)'", view))
    assert emitted <= named, f"no action for: {sorted(emitted - named)}"


def test_a_shipment_still_at_sea_is_not_listed_as_a_fault():
    """Flagging the normal state of a shipment teaches people to
    ignore the panel. Eight identical rows teaches them faster."""
    block = PLATFORM[PLATFORM.index("(spec.panels || []).forEach"):]
    block = block[:block.index("const rows = data[")]
    assert "const live = found.filter((r) => !r.expected)" in block
    assert "found.filter((r) => r.expected)" in block
    assert "live.forEach" in block, "expected rows are still a row each"
    assert "normal state of a shipment" in block


def test_the_corrective_action_gets_the_width_and_not_the_reference():
    """The table gave the shipment reference 770px and squeezed
    "Enter the volume of each received line on this shipment" into
    sixty, one word a line, eleven lines deep. Seen in a browser."""
    block = CSS[CSS.index(".exceptions td:not(:last-child)"):]
    block = block[:block.index("\n.exceptions td:last-child")]
    assert "width: 1%" in block and "nowrap" in block
    last = CSS[CSS.index(".exceptions td:last-child"):]
    last = last[:last.index("}")]
    assert "min-width" in last and "white-space: normal" in last


# ------------------------------------- a finding that goes somewhere


def test_every_dataset_a_check_can_name_has_a_screen_to_fix_it_on():
    """A finding reading "demand history for items that are not in the
    stock file" leaves somebody to work out which of nineteen screens
    that means. One they cannot act on in a step is one they read once
    and never act on at all."""
    datasets = set(re.findall(r'"[a-z_]+", (?:BLOCKS|WARNS), "([a-z_ ]+)"',
                              SANITY))
    assert datasets, "the pattern no longer matches how findings are built"
    block = PLATFORM[PLATFORM.index("      fixAt: (r) => ({"):]
    block = block[:block.index("}[String(r.dataset")]
    routed = set(re.findall(r"^\s+([a-z_]+):", block, re.M))
    assert datasets <= routed, f"nowhere to fix: {sorted(datasets - routed)}"


def test_the_route_is_taken_when_the_row_has_no_drawer_of_its_own():
    block = PLATFORM[PLATFORM.index("        onOpen: spec.opens"):]
    block = block[:block.index("      }));")]
    assert "spec.fixAt" in block
    assert "location.hash" in block
