"""What the import preview shows about what is already recorded.

The server has answered this since the receipt-correction work went in
-- counts per table, what changed, the recorded figure beside the
incoming one -- and nothing on the screen read it. A person saw how
many rows were in their file and pressed Commit.

Rendering was checked in a browser against a real double import with a
receipt and an item deliberately made to disagree. These pin the parts
that can drift back.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "web" / "static" / "app.js").read_text()
IMPORTER = (ROOT / "engine" / "importer.py").read_text()


def test_the_preview_reads_what_the_server_says_about_recorded_rows():
    assert "againstRecorded(host, d.against_recorded)" in JS


def test_every_fate_the_importer_can_report_has_words_on_screen():
    """The drift that matters. A new `action` in _specs with no entry
    in FATE renders a sheet with no explanation of what Commit will do
    to it -- and the whole point of the screen is that the three fates
    are different."""
    actions = set(re.findall(r'action="([a-z]+)"', IMPORTER))
    fate = JS[JS.index("const FATE = {"):]
    fate = fate[:fate.index("\n};")]
    named = set(re.findall(r"^\s{2}([a-z]+):", fate, re.M))
    assert actions <= named, f"no wording for: {sorted(actions - named)}"


def test_the_three_fates_are_told_apart_not_merged():
    """Safe update, new rows added, and something that already
    happened. A receipt moved stock and a valuation took its cost; a
    later file saying it was really ninety does not unhappen that."""
    fate = JS[JS.index("const FATE = {"):]
    fate = fate[:fate.index("\n};")]
    assert "Committing changes these to match your file" in fate
    assert "leaves the rest as they are" in fate
    # The string is wrapped across source lines, so compare the
    # words rather than the literal.
    assert "Correcting one is its own" in fate and "entry" in fate


def test_rows_needing_a_correction_are_counted_on_their_own():
    """Folded into one 'changed' figure beside a Commit button, they
    read as something the commit deals with."""
    block = JS[JS.index("function againstRecorded("):]
    block = block[:block.index("\n/* A row of values")]
    assert "needs_correction" in block
    assert "Needs a correction" in block


def test_the_fate_is_explained_once_rather_than_on_every_row():
    """Twenty-one rows of the same sentence squeezed the column to
    ninety pixels, and the Goods received row -- whose sentence is the
    long one, and the one that matters -- wrapped into a
    four-hundred-pixel ribbon of two-word lines. Seen in a browser."""
    block = JS[JS.index("function againstRecorded("):]
    block = block[:block.index("\n/* A row of values")]
    body = block[block.index("busy.map("):block.index("const fates")]
    assert "why" not in body, "the explanation is back inside the row"
    assert "new Set(busy.map((t) => t.action))" in block


def test_the_report_does_not_centre_its_own_notes():
    """.drop-sub is `margin: 0 auto; max-width: 54ch`, built to sit
    centred under the drop zone. Used in a 1,194px report it centres a
    389px column of text under a left-aligned heading, which reads as
    a layout fault. Measured in a browser."""
    block = JS[JS.index("function importReport("):]
    block = block[:block.index("\n/* A free instance sleeps")]
    # Comments name the class to explain why it is not used, so look
    # at the code rather than the prose.
    code = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "drop-sub" not in code


def test_a_missing_value_is_not_shown_as_a_zero():
    """Not entered and entered as nothing are different facts, and
    this screen is where somebody decides whether a figure is wrong."""
    fmt = JS[JS.index("function fmtVals("):]
    fmt = fmt[:fmt.index("\n}")]
    assert "=== null" in fmt and "'—'" in fmt
