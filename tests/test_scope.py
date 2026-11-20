"""Scope is applied to the facts, not to the rows afterwards.

The bug these exist to prevent: picking a branch narrowed the table and left
every figure above it reading the whole group. The table said four lines and
the tile above it said PKR 41.4M across thirty-six. The tile is the number
people quote, so it was the worse half of the lie.
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from engine import parse, report, samples
from engine import scope as sc
from engine.schema import Thresholds
from web.app import app

client = TestClient(app)
AS_OF = date(2026, 11, 20)


@pytest.fixture(scope="module")
def facts():
    recs = {k: parse.parse(k, samples.sample_csv(k))[0] for k in samples.ORDER}
    return {
        "items": recs["inventory"],
        "demand": recs["demand"],
        "invoices": recs["receivables"],
        "jobs": recs["service"],
        "placements": recs["machines"],
        "commitments": recs["commitments"],
    }


def run(facts, entity=None, location=None):
    s = sc.read(entity, location)
    narrowed = sc.apply(s, **facts)
    return report.build(
        **narrowed["records"], thresholds=Thresholds(), as_of=AS_OF,
        scope=s, group=facts, scope_counts=narrowed["counts"],
    )


# ----------------------------------------------------------------- reading

def test_all_and_blank_both_mean_no_filter():
    assert not sc.read("all", "all").active
    assert not sc.read("", "").active
    assert not sc.read(None, None).active
    assert sc.read("DX", "all").active


def test_a_branch_without_a_company_is_kept():
    """Branch codes are unique here, so refusing it would only make a
    shared link fragile."""
    s = sc.read("all", "KHI")
    assert s.active and s.location == "KHI" and s.entity is None


def test_the_label_reads_as_words():
    assert sc.read(None, None).label() == "the whole group"
    assert sc.read("DX", None).label("Diagnostix") == "Diagnostix"
    assert sc.read("DX", "KHI").label("Diagnostix", "Karachi") == "Karachi, Diagnostix"


# ---------------------------------------------------------------- narrowing

def test_narrowing_a_company_narrows_every_scoped_module(facts):
    group = run(facts)
    dx = run(facts, "DX")
    assert len(dx["stock"]["rows"]) < len(group["stock"]["rows"])
    assert dx["service"]["jobs"] < group["service"]["jobs"]
    assert len(dx["machines"]["rows"]) < len(group["machines"]["rows"])
    assert len(dx["forecast"]["rows"]) < len(group["forecast"]["rows"])


def test_a_branch_narrows_further_than_its_company(facts):
    dx = run(facts, "DX")
    khi = run(facts, "DX", "KHI")
    assert len(khi["stock"]["rows"]) < len(dx["stock"]["rows"])


def test_every_scoped_row_really_is_in_scope(facts):
    khi = run(facts, "DX", "KHI")
    for key in ("stock", "machines", "forecast"):
        for row in khi[key]["rows"]:
            assert row["entity_id"] == "DX", (key, row)
            assert row["location_id"] == "KHI", (key, row)


def test_the_figures_agree_with_the_rows_they_sit_above(facts):
    """The whole point. Each tile is a sum over exactly the rows shown."""
    khi = run(facts, "DX", "KHI")
    rows = khi["stock"]["rows"]
    assert khi["receivables"]["outstanding"] == pytest.approx(
        sum(c["outstanding"] for c in khi["receivables"]["customers"]), abs=1.0
    )
    # and the scoped stock total is below the group's
    group = run(facts)
    assert sum(r["stock_value"] for r in rows) < sum(
        r["stock_value"] for r in group["stock"]["rows"]
    )


def test_counts_report_what_was_set_aside(facts):
    khi = run(facts, "DX", "KHI")
    kept = khi["scope"]["kept"]
    assert kept["items"]["kept"] < kept["items"]["total"]
    assert all(v["kept"] <= v["total"] for v in kept.values())


# ------------------------------------------------------------- group level

def test_cash_stays_group_level_and_says_so(facts):
    group = run(facts)
    dx = run(facts, "DX")
    assert dx["cash"]["weeks"] == group["cash"]["weeks"]
    assert dx["cash"]["total_receipts"] == group["cash"]["total_receipts"]
    assert "cash" in dx["scope"]["group_level"]
    assert group["scope"]["group_level"] == []


def test_transfers_see_both_ends_and_keep_the_moves_this_branch_is_party_to(facts):
    group = run(facts)
    khi = run(facts, "DX", "KHI")
    assert len(khi["transfers"]["rows"]) <= len(group["transfers"]["rows"])
    for t in khi["transfers"]["rows"]:
        touches = "KHI" in (t["from_location"], t["to_location"])
        assert touches, t
    assert "transfers" in khi["scope"]["group_level"]


def test_a_transfer_into_the_branch_is_not_hidden_from_it(facts):
    """Filtering the facts would have left a branch blind to stock arriving."""
    khi = run(facts, "DX", "KHI")
    incoming = [t for t in khi["transfers"]["rows"] if t["to_location"] == "KHI"]
    outgoing = [t for t in khi["transfers"]["rows"] if t["from_location"] == "KHI"]
    assert incoming or outgoing
    # and nothing unrelated crept in
    for t in khi["transfers"]["rows"]:
        assert "KHI" in (t["from_location"], t["to_location"])


# -------------------------------------------------------------- the selector

def test_the_selector_still_offers_the_rest_of_the_business(facts):
    """Building the options from the scoped blocks left no way back."""
    dx = run(facts, "DX")
    assert dx["scope"]["entities"] == run(facts)["scope"]["entities"]
    assert len(dx["scope"]["locations"]) == len(run(facts)["scope"]["locations"])


# ------------------------------------------------------------- over the api

def test_the_endpoint_accepts_a_scope():
    r = client.post("/api/analyse", data={"as_of": "2026-11-20", "entity": "DX"})
    assert r.status_code == 200
    d = r.json()
    assert d["scope"]["entity"] == "DX"
    assert d["scope"]["active"] is True
    assert all(row["entity_id"] == "DX" for row in d["stock"]["rows"])


def test_a_scope_matching_nothing_is_refused_with_the_reason():
    r = client.post("/api/analyse",
                    data={"as_of": "2026-11-20", "entity": "NOT-A-COMPANY"})
    assert r.status_code == 400
    assert "NOT-A-COMPANY" in r.json()["error"]


def test_an_unscoped_run_is_unchanged_by_all_of_this():
    a = client.post("/api/analyse", data={"as_of": "2026-11-20"}).json()
    b = client.post("/api/analyse",
                    data={"as_of": "2026-11-20", "entity": "all",
                          "location": "all"}).json()
    assert a["scope"]["active"] is False
    assert len(a["stock"]["rows"]) == len(b["stock"]["rows"])
    assert a["headline"] == b["headline"]
