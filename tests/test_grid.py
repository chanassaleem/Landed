"""The shared grid: sorting, and knowing what can be typed into.

Every module screen draws its table through this component, so what it
can and cannot do is what fourteen screens can and cannot do.

Both were driven in a browser against an imported workbook. Sorting by
On hand reorders the rows, sets aria-sort, survives a reload and cycles
back to the order the server sent. Clicking the Item cell opens an
editor, Enter saves it, the server is called and the cell confirms.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
GRID = (ROOT / "web" / "static" / "grid.js").read_text()
CSS = (ROOT / "web" / "static" / "app.css").read_text()


# ------------------------------------- sorting


def test_a_column_heading_sorts_by_it():
    """The grid had none. For fourteen screens a heading was something
    to drag or resize and not something to order by, which is the first
    thing anybody does to a table of two hundred rows."""
    assert "function sortBy(" in GRID
    assert "aria-sort" in GRID
    assert "label.addEventListener('click', () => sortBy(c.key))" in GRID


def test_the_heading_is_a_button_so_the_keyboard_reaches_it():
    """And so a click at the end of a drag meant to move the column
    does not sort it instead: the th is already draggable and carries
    the resize handle."""
    block = GRID[GRID.index("const label = el('button', 'th-label');"):]
    block = block[:block.index("th.append(label);")]
    assert "label.type = 'button'" in block
    assert "aria-label" in block


def test_an_empty_cell_sorts_last_whichever_way_the_column_points():
    """A missing figure is not a zero and must not travel with the
    zeros. An item with no demand yet and an item selling nothing are
    different facts, and this product spends a lot of effort keeping
    them apart."""
    block = GRID[GRID.index("function compare("):GRID.index("function sortBy(")]
    assert "xEmpty" in block and "yEmpty" in block
    assert "(xEmpty ? 1 : -1)" in block, "empties do not sort last"
    assert "if (xEmpty !== yEmpty) return by;" in block, (
        "the direction is applied to the empties too, so they move")


def test_numbers_sort_as_numbers_and_codes_sort_naturally():
    """A plain string sort puts RGT-10 before RGT-2, and a list of SKUs
    is where that looks most obviously broken."""
    block = GRID[GRID.index("function compare("):GRID.index("function sortBy(")]
    assert "NUMERIC_SORT.has(c.fmt)" in block
    assert "numeric: true" in block


def test_sorting_does_not_reorder_the_caller_s_own_rows():
    block = GRID[GRID.index("function sorted("):GRID.index("function sortBy(")]
    assert "[...body].sort" in block


def test_a_third_click_returns_to_the_order_the_server_sent():
    """Usually the one that matters most, so it has to be reachable."""
    block = GRID[GRID.index("function sortBy("):]
    block = block[:block.index("\n    }")]
    assert "was === 'descending' ? null" in block


def test_the_sort_is_saved_under_the_grid_and_not_the_column():
    """Named `key`, the parameter shadowed the grid's own key -- the
    one every other save in here uses -- so the sort was written to
    localStorage under the name of the column. It survived the click
    and vanished on reload, leaving a stray `landed.grid.on_hand`
    entry as the only sign."""
    block = GRID[GRID.index("function sortBy("):]
    block = block[:block.index("\n    }")]
    assert "function sortBy(column)" in block, "the parameter shadows `key`"
    assert "save(key, state)" in block


def test_there_is_one_sort_indicator_and_not_two():
    """`.panel th[aria-sort]::after` already drew a caret for the
    hand-built tables, and the grid lives inside .panel -- so setting
    aria-sort on it gave every sorted heading an arrow and a triangle
    underneath."""
    assert ".grid-table th[aria-sort]::after { content: none; }" in CSS
    assert "\\2191" not in CSS and "\\2193" not in CSS, (
        "a second arrow glyph is back")


# ------------------------------------- what can be typed into


def test_the_columns_that_accept_typing_are_marked_in_their_headings():
    """It was marked on the cell and only on hover. Of the eight
    columns the stock screen shows by default, exactly one is editable
    and nothing on screen said so -- the capability existed and could
    not be found."""
    assert "th.dataset.editable = '1'" in GRID
    assert ".grid-table th[data-editable='1'] .th-label" in CSS


def test_the_table_says_what_it_lets_you_correct():
    block = GRID[GRID.index("const sayEditable = ()"):]
    block = block[:block.index("\n    };")]
    assert "to correct" in block
    assert "Columns" in block, (
        "it does not say the editable columns can be shown")
    assert ".grid-note {" in CSS


def test_the_hover_cue_is_not_the_colour_of_the_table_s_own_lines():
    """--rule is the hairline between every cell, so the cue that a
    cell could be typed into read as nothing at all."""
    line = [l for l in CSS.splitlines() if "td.can-edit:hover" in l][0]
    assert "--focus" in line and "--rule" not in line


def test_a_save_leaves_a_mark_and_so_does_a_refusal():
    """A refusal was marked and a success was not -- and this is the
    kind of save that can be triggered by clicking away, which is the
    easiest kind to miss."""
    assert "td.dataset.saved = '1'" in GRID
    assert "td.dataset.refused = '1'" in GRID
    assert "td[data-saved='1']" in CSS
    assert "td[data-refused='1']" in CSS


def test_a_refused_edit_puts_the_old_value_back():
    """A cell that keeps a value the server refused is a screen
    quietly disagreeing with the database."""
    block = GRID[GRID.index("if (ok === false) {"):]
    block = block[:block.index("} else {")]
    assert "row[col.key] = was" in block
