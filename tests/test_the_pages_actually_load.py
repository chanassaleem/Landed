"""Boot every screen in a real browser and insist nothing throws.

Written after a deletion removed six superseded screens and, with
them, five helpers the surviving code still called -- `empty`,
`table`, `cell`, `palette`, `renderAll` -- plus two top-level `let`
declarations that happened to sit inside the deleted ranges. The
whole suite stayed green and the commit was pushed, because 1,088
tests exercised the Python and not one of them opened the page. The
first line of JavaScript to run threw a ReferenceError and every
screen rendered blank.

`node --check` does not catch this: the file parses. A name that is
never declared is only an error when the line referencing it runs.
So the check has to be a browser that runs them.

What it asserts is deliberately shallow -- the page loads, each
screen draws something, nothing lands in the console -- because a
deep assertion here duplicates the module tests and breaks on every
copy change. The value is in the breadth, not the depth.

Skipped where Playwright or its browser is not installed, so nobody
is blocked from running the suite; CI installs both.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DSN = os.environ.get("LANDED_TEST_DSN")

sync_playwright = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright is not installed",
).sync_playwright


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def served():
    """The dev server, on its own database, signed in.

    Its own database because the seed drops and rebuilds the schema,
    and doing that under the database the rest of the suite is using
    would pull the floor out from the tests running beside it.
    """
    if not DSN:
        pytest.skip("LANDED_TEST_DSN not set")

    import psycopg
    from psycopg import conninfo

    parts = conninfo.conninfo_to_dict(DSN)
    admin = conninfo.make_conninfo(**{**parts, "dbname": "postgres"})
    name = "landed_pages"
    with psycopg.connect(admin, autocommit=True) as con:
        con.execute(f'drop database if exists "{name}" with (force)')
        con.execute(f'create database "{name}"')
    page_dsn = conninfo.make_conninfo(**{**parts, "dbname": name})

    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": page_dsn,
        "LANDED_TEST_DSN": page_dsn,
        # The sign-in gate asks the server whether there is anything
        # to sign in to before it draws a form. The dev server stubs
        # the verifier but not this, so without them every screen is
        # the "sign-in is not switched on" card.
        "SUPABASE_URL": "https://dev.invalid",
        "SUPABASE_ANON_KEY": "dev-anon-key",
    }
    proc = subprocess.Popen(
        [sys.executable, "tools/dev_server.py", str(port)],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    base = f"http://127.0.0.1:{port}"
    for _ in range(600):                      # the seed builds a schema
        if proc.poll() is not None:
            pytest.fail("dev server died:\n" + (proc.stdout.read() or ""))
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("dev server never came up")
    time.sleep(0.5)
    try:
        yield base
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture(scope="module")
def page(served):
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:                # noqa: BLE001
            pytest.skip(f"no browser: {e}")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        pg = ctx.new_page()
        pg.goto(f"{served}/sign-in")
        pg.request.post(f"{served}/auth/sign-in",
                        data={"email": "owner@seed.test", "password": "x"})
        yield pg, served
        browser.close()


# Everything in the rail, plus the screens that are not modules.
SCREENS = [
    "today", "home", "import", "settings",
    "m/stock", "m/buy", "m/freight", "m/cash", "m/owed", "m/equipment",
    "m/quality", "m/people",
]


def test_every_screen_loads_without_a_single_console_error(page):
    pg, base = page
    noise: list[str] = []
    pg.on("console", lambda m: m.type == "error" and noise.append(m.text))
    pg.on("pageerror", lambda e: noise.append(f"uncaught: {e}"))

    pg.goto(f"{base}/", wait_until="networkidle")
    pg.wait_for_timeout(1200)
    blank = []
    for where in SCREENS:
        pg.evaluate(f"location.hash = '#{where}'")
        pg.wait_for_timeout(900)
        drawn = pg.evaluate(
            "(document.querySelector('.view.on')?.innerText || '').trim().length")
        if drawn < 20:
            blank.append(where)

    assert not noise, (
        "the browser console is not clean:\n  " + "\n  ".join(dict.fromkeys(noise)))
    assert not blank, f"these screens drew nothing: {blank}"


def test_an_address_that_no_longer_exists_lands_on_its_replacement(page):
    """Somebody's bookmark, or a link in an old email."""
    pg, base = page
    pg.goto(f"{base}/#cash", wait_until="networkidle")
    pg.wait_for_timeout(1500)
    assert pg.evaluate("location.hash") == "#m/cash"


def test_the_back_button_goes_back(page):
    """There was no hash router at all, so it did nothing anywhere."""
    pg, base = page
    pg.goto(f"{base}/#m/stock", wait_until="networkidle")
    pg.wait_for_timeout(1400)
    pg.evaluate("location.hash = '#m/freight'")
    pg.wait_for_timeout(1200)
    assert pg.evaluate("location.hash") == "#m/freight"
    pg.go_back()
    pg.wait_for_timeout(1200)
    assert pg.evaluate("location.hash") == "#m/stock"
    # Scoped to the visible view. Unscoped, this picks up the Settings
    # heading, which sits earlier in the document and is always in the
    # DOM whether or not that screen is the one on show.
    heading = pg.evaluate(
        "document.querySelector('.view.on .section-head h2')"
        "?.textContent || ''")
    assert "Stock" in heading, (
        f"the address went back but the screen did not: {heading!r}")


def test_the_command_palette_offers_the_modules_that_exist(page):
    """It offered six screens that had been deleted, and opening one
    showed a page whose own heading read "Cash is not connected yet"
    -- beside a working Cash module in the rail."""
    pg, base = page
    pg.goto(f"{base}/#today", wait_until="networkidle")
    pg.wait_for_timeout(1200)
    pg.keyboard.press("Meta+k")
    pg.wait_for_timeout(400)
    if pg.evaluate("document.getElementById('scrim')?.hidden !== false"):
        pg.keyboard.press("Control+k")
        pg.wait_for_timeout(400)
    offered = pg.evaluate(
        "[...document.querySelectorAll('#results li, #results .item')]"
        ".map(e => e.textContent)")
    assert offered, "the palette offered nothing at all"
    text = " ".join(offered)
    assert "Go to Stock" in text, f"no stock module in the palette: {offered[:12]}"


def test_the_money_band_opens_the_screen_that_holds_each_segment(page):
    """Its segments used to filter a stock list that no longer exists."""
    pg, base = page
    pg.goto(f"{base}/#today", wait_until="networkidle")
    pg.wait_for_timeout(1400)
    # The palette from the test above shares this page and its scrim
    # swallows clicks. Closing it here rather than there, so this test
    # stands on its own if the order ever changes.
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)
    seg = pg.locator("[aria-pressed]").first
    if not seg.count():
        pytest.skip("the money band drew no segments on this seed")
    seg.click()
    pg.wait_for_timeout(1200)
    assert pg.evaluate("location.hash").startswith("#m/"), (
        "clicking the band went nowhere")
