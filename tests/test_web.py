"""The web layer: the page, the payload, and the model download.

The engine is tested on its own. These are about the seam -- that the
figures reaching the screen are the ones the engine produced, that the
money line adds up, and that the page does not quietly claim to be
something it is not.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web.app import app

client = TestClient(app)


@pytest.fixture(scope="module")
def payload():
    r = client.get("/api/state")
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------- the page


def test_the_page_loads():
    r = client.get("/")
    assert r.status_code == 200
    assert "<title>Landed</title>" in r.text


def test_the_page_says_the_figures_are_invented(payload):
    """It is sample data until somebody's own data is in it.

    A demo that does not say so is how a prospect quotes your invented
    numbers back at you in a meeting.
    """
    assert payload["source"] == "Sample data"
    assert "Sample data" in client.get("/").text


def test_the_theme_is_set_before_first_paint():
    """Otherwise a dark-mode user gets a white flash on every load."""
    body = client.get("/").text
    assert "landed.theme" in body
    head = body[: body.index("</head>")]
    assert "prefers-color-scheme: dark" in head


def test_health_needs_no_database():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


# ----------------------------------------------------------- the payload


def test_the_money_line_adds_up_to_what_it_claims(payload):
    """A band whose parts do not sum to its whole is a chart people stop
    trusting the moment they check it, and they do check it."""
    line = payload["line"]
    parts = sum(line.values())
    shelf = sum(
        r["value"] for r in payload["stock"]
    )
    in_transit = sum(s["goods"] + s["landed_cost"] for s in payload["shipments"])
    assert parts == pytest.approx(shelf + in_transit, rel=1e-6)


def test_idle_money_is_only_the_part_above_the_limit(payload):
    """Not the whole over-stocked line.

    Most of an over-stocked line is stock the business genuinely needs.
    Counting all of it as standing still overstates the problem, and an
    overstated problem gets ignored.
    """
    for r in payload["stock"]:
        assert r["idle_value"] <= r["value"] + 1e-6


def test_every_row_carries_the_branch_it_belongs_to(payload):
    """Without it the branch filter is a lie and so is every total."""
    assert payload["stock"]
    for r in payload["stock"]:
        assert r["branch"]
        assert r["branch"] in payload["branches"]


def test_the_state_words_are_ones_a_person_would_use(payload):
    allowed = {
        "running out", "reorder", "standing still", "expiring", "within policy",
    }
    assert {r["state"] for r in payload["stock"]} <= allowed


def test_every_row_belongs_to_a_segment_of_the_money_line(payload):
    """The band is the filter, so a row in no segment is unreachable."""
    known = set(payload["line"])
    for r in payload["stock"]:
        assert r["segments"]
        assert set(r["segments"]) <= known


def test_cover_is_either_a_number_or_honestly_absent(payload):
    """No demand means no cover figure. Zero would read as 'runs out
    today', which is the opposite of what it means."""
    for r in payload["stock"]:
        assert r["cover"] is None or r["cover"] >= 0


def test_at_most_three_decisions(payload):
    """A list of twelve things to decide is a list nobody reads."""
    assert len(payload["decisions"]) <= 3
    for d in payload["decisions"]:
        assert d["segment"] in payload["line"]
        assert d["body"] and d["action"]


def test_shipments_admit_their_freight_is_an_estimate(payload):
    """Until the clearing agent invoices, the landed cost is a guess,
    and a guess shown as a fact is the thing that loses trust."""
    for s in payload["shipments"]:
        assert s["estimated"] is True


def test_the_payload_matches_what_the_engine_computed(payload):
    """The seam this file exists for.

    If the API quietly re-derives a figure the engine already produced,
    the two drift and nobody notices until a customer asks why the
    screen and the spreadsheet disagree.
    """
    from datetime import date

    from engine import parse as parser
    from engine import report, samples
    from engine.schema import Thresholds

    recs = {
        n: parser.parse(n, samples.sample_csv(n))[0] for n in ("inventory", "demand")
    }
    blocks = report.build_blocks(
        recs["inventory"], recs["demand"], Thresholds(), samples.AS_OF
    )
    engine_rows = {
        (r.item_key, b.location_id): r for b in blocks for r in b.results
    }

    assert len(payload["stock"]) == len(engine_rows)
    for row in payload["stock"]:
        r = engine_rows[(row["sku"], row["branch"])]
        assert row["on_hand"] == pytest.approx(r.on_hand)
        assert row["value"] == pytest.approx(r.stock_value)
        assert row["idle_value"] == pytest.approx(r.excess_value)
        assert row["reorder_point"] == pytest.approx(r.reorder_point)


# ------------------------------------------------------------- the model


def test_the_excel_model_downloads():
    r = client.get("/api/workbook.xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats"
    )
    assert "landed-model.xlsx" in r.headers["content-disposition"]
    assert r.content[:2] == b"PK"      # a real zip, which is what xlsx is


def test_every_decision_tone_has_a_colour_in_the_stylesheet(payload):
    """A tone the stylesheet does not know falls back to grey, and a grey
    accent on an urgent decision is a silent failure -- nothing errors,
    the page renders, and the one signal saying how bad this is has been
    quietly removed."""
    import pathlib
    import re

    css = pathlib.Path("web/static/app.css").read_text()
    known = set(re.findall(r"\.d-([a-z]+)\s*\{", css))
    used = {d["tone"] for d in payload["decisions"]}
    assert used <= known, f"no .d-* rule for {sorted(used - known)}"
    assert not known - used - {"eck"}, f"dead rules for {sorted(known - used)}"
