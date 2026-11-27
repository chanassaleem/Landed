"""What the forms that write to the ledger say before they write.

Driven in a browser: the context line reading "CON-1365A Control serum
level A at LHR · 1 on the shelf", the count hint naming the ledger's
figure, a note turning required when the count moved 172 away from it,
and the transfer naming both ends and capping the quantity at what is
actually on the shelf.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "web" / "static" / "app.js").read_text()
CSS = (ROOT / "web" / "static" / "app.css").read_text()


def _op(key: str) -> str:
    start = JS.index(f"key: '{key}',")
    return JS[start:JS.index("send: (row, v)", start)]


def test_every_form_names_the_line_and_the_branch_it_is_about():
    """The drawer said both and the form replaced the drawer, so from
    the moment somebody started typing there was nothing on screen
    naming what they were typing about. On a stock list of hundreds
    that is one mis-click from receiving the wrong reagent."""
    block = JS[JS.index("function openForm(op, row) {"):]
    block = block[:block.index("const form = el('form', 'op-form');")]
    assert "op-context" in block
    assert "row.sku" in block and "row.branch" in block
    assert "on the shelf" in block, "it does not say what is there"
    assert ".op-context {" in CSS


def test_a_count_shows_the_figure_it_is_reconciling_against():
    op = _op("adjust")
    assert "The ledger says" in op
    assert "what you actually counted" in op


def test_a_count_that_moves_the_figure_materially_has_to_say_why():
    """A tenth is somebody finding a box. Half is a theft, a breakage
    or a miscount, and in three months the note is the only thing that
    will say which."""
    op = _op("adjust")
    assert "gap / was >= 0.1" in op
    assert "required = big" in op
    assert "breakage" in op


def test_a_small_count_difference_does_not_demand_an_explanation():
    """A box found is not an incident, and a rule that asks on every
    count is a rule people fill with a full stop."""
    op = _op("adjust")
    assert "required = false" in op.replace("required = false",
                                            "required = false")
    assert "fields.note.input.required = false" in op


def test_a_transfer_says_what_can_go_before_the_server_refuses():
    """A refusal that arrives on submit is one somebody has already
    spent a minute earning."""
    op = _op("transfer")
    assert "on the shelf at" in op
    assert "input.max = String(can)" in op
    assert "nothing to send from here yet" in op, (
        "an empty branch gets the same message as a full one")


def test_a_transfer_names_both_ends():
    op = _op("transfer")
    assert "Out of ${row.branch}, into" in op
    assert "both or neither" in op


def test_an_expired_session_does_not_read_as_a_failed_write():
    """Nothing was recorded and what they typed is still in the form.
    Saying both is the difference between signing in again and
    retyping a count of forty-three batches."""
    block = JS[JS.index("if (r.status === 401) {"):]
    block = block[:block.index("const { data } = await readResponse")]
    assert "nothing was recorded" in block
    assert "still here" in block
