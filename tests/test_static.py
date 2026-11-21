"""The browser files must parse.

A syntax error in app.js is invisible to every other test here: the
server still returns 200 for the page and for every endpoint, the HTML
is unchanged, and the only symptom is a blank screen in a browser
nobody opened. This runs the files through a real parser.

It happened: an edit left `async` behind on the line above the function
it belonged to, and the suite stayed green.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "web" / "static"
SCRIPTS = sorted(STATIC.glob("*.js"))


def test_there_are_scripts_to_check():
    """Guards the guard: a glob that matches nothing passes silently."""
    assert SCRIPTS, f"no .js files found in {STATIC}"


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_the_script_parses(path):
    r = subprocess.run(["node", "--check", str(path)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("path", sorted(STATIC.glob("*.html")),
                         ids=lambda p: p.name)
def test_every_script_and_stylesheet_the_page_asks_for_exists(path):
    """A 404 on a stylesheet is an unstyled page, and the server log
    says 404 next to a filename nobody is looking at."""
    import re

    body = path.read_text()
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', body)
    assert refs, f"{path.name} references nothing"
    for ref in refs:
        target = STATIC / ref[len("/static/"):]
        assert target.exists(), f"{path.name} asks for {ref}, which is missing"
