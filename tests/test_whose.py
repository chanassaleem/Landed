"""Whose business this is, and what you may do in it.

One person is one business now, so there is nothing to switch between
and no menu pretending otherwise. But a figure on screen belongs to
somebody, and an importer running two companies on two logins should
never have to work out which one they are looking at.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "web" / "static" / "index.html").read_text()
CSS = (ROOT / "web" / "static" / "app.css").read_text()
JS = (ROOT / "web" / "static" / "app.js").read_text()


def test_the_header_has_somewhere_to_say_whose_it_is():
    assert 'id="whose"' in HTML
    assert 'id="whose-org"' in HTML
    assert 'id="whose-role"' in HTML


def test_it_starts_hidden_so_it_never_flashes_a_wrong_name():
    block = HTML[HTML.index('id="whose"'):HTML.index('id="whose-org"')]
    assert "hidden" in block
    assert "host.hidden = false" in JS, "and is only shown once known"


def test_the_role_comes_back_with_the_organisation():
    """One question -- whose business is this and what may I do in it --
    so one answer. A person who cannot receive stock should see why
    from the chrome rather than from a button that does nothing."""
    app = (ROOT / "web" / "app.py").read_text()
    me = app[app.index('@app.get("/api/me")'):]
    me = me[:me.index('@app.post("/api/organisation")')]
    assert "m.role" in me
    assert "status = 'active'" in me, (
        "a suspended membership is not a role in a business")
    assert '"role": r' in me


def test_a_long_business_name_cannot_push_the_search_box_off_the_bar():
    """A supplier in this trade can be called Shenzhen Mindray
    Bio-Medical Electronics Co., Ltd., and so can a customer's own
    company."""
    block = CSS[CSS.index(".whose-org {"):]
    block = block[:block.index("}")]
    assert "text-overflow: ellipsis" in block
    assert "max-width" in block
    assert "title = org.name" in JS, "the full name has to be readable"


def test_it_gives_way_to_search_on_a_phone():
    """Below 560px the bar cannot hold both. The whole thing goes, not
    just the name: a pill reading "owner" with nothing beside it says
    who you are to a business it no longer names, which is less use
    than saying nothing. Seen in a browser at 430px before it was."""
    assert re.search(r"@media \(max-width: 560px\) \{\s*\.whose "
                     r"\{ display: none; \}", CSS)


def test_the_role_reads_as_a_role_to_a_screen_reader():
    """'owner' beside a company name is a word, not a role, when it
    arrives with no context."""
    assert "your role:" in JS


# The check that every var() resolves covers the whole stylesheet,
# including this block, and lives in test_responsive.py. Two copies of
# one regex is two chances for it to be subtly wrong in one of them.
