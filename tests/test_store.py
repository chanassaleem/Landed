"""Tests for the DuckDB store.

The store's job is to never lose, never duplicate, and never mutate a fact.
These tests are about those three properties, not about SQL.
"""

from datetime import date

import pytest

from engine.db.store import DuplicateUpload, Store
from engine.io import load_history, load_items
from engine.modules.classify import classify
from engine.modules.inventory import evaluate
from engine.schema import Thresholds

INV = "tests/data/inventory.csv"
HIST = "tests/data/demand_history.csv"


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def loaded(store):
    items = load_items(INV)
    hist = load_history(HIST)
    store.upsert_items(items)
    sid = store.add_snapshot(
        report_type="inventory", as_of=date(2026, 10, 16),
        source_filename="inventory.csv", source_bytes=open(INV, "rb").read(),
        row_count=len(items),
    )
    store.add_inventory_facts(sid, items)
    hid = store.add_snapshot(
        report_type="demand", as_of=date(2026, 10, 16),
        source_filename="demand.csv", source_bytes=open(HIST, "rb").read(),
        row_count=len(hist),
    )
    store.add_demand_facts(hid, hist)
    return store, sid, hid, items, hist


def test_schema_creates_cleanly_and_is_idempotent():
    s = Store(":memory:")
    s._init_schema()          # running it twice must not fail
    assert s.stats()["snapshots"] == 0
    s.close()


def test_round_trip_preserves_every_row(loaded):
    store, sid, hid, items, hist = loaded
    assert len(store.read_items(sid)) == len(items)
    assert len(store.read_demand(hid)) == len(hist)


def test_round_trip_preserves_values_exactly(loaded):
    store, sid, _, items, _ = loaded
    got = {i.item_key: i for i in store.read_items(sid)}
    for want in items:
        g = got[want.item_key]
        assert g.on_hand == want.on_hand
        assert g.unit_cost == want.unit_cost
        assert g.lead_time_days == want.lead_time_days
        assert g.lead_time_days_std == want.lead_time_days_std
        assert g.expiry_date == want.expiry_date
        assert g.is_clinical_critical == want.is_clinical_critical


def test_engine_gives_identical_results_from_db_or_from_csv(loaded):
    """Persistence must not change a single number."""
    store, sid, hid, items, hist = loaded
    th = Thresholds()
    as_of = date(2026, 10, 16)

    from_csv = evaluate(items, classify(items, hist, th), th, as_of=as_of)
    db_items, db_hist = store.read_items(sid), store.read_demand(hid)
    from_db = evaluate(db_items, classify(db_items, db_hist, th), th, as_of=as_of)

    assert len(from_csv) == len(from_db)
    for a, b in zip(from_csv, from_db):
        assert a.item_key == b.item_key
        assert a.status == b.status
        assert a.safety_stock == pytest.approx(b.safety_stock)
        assert a.reorder_point == pytest.approx(b.reorder_point)
        assert a.suggested_order_qty == pytest.approx(b.suggested_order_qty)


def test_identical_file_is_refused_as_a_duplicate(store):
    data = open(INV, "rb").read()
    store.add_snapshot(report_type="inventory", as_of=date(2026, 10, 16),
                       source_bytes=data)
    with pytest.raises(DuplicateUpload):
        store.add_snapshot(report_type="inventory", as_of=date(2026, 10, 17),
                           source_bytes=data)


def test_reupload_supersedes_rather_than_overwrites(store):
    items = load_items(INV)
    first = store.add_snapshot(report_type="inventory", as_of=date(2026, 10, 16),
                               source_bytes=b"version one", row_count=10)
    store.add_inventory_facts(first, items)

    second = store.add_snapshot(report_type="inventory", as_of=date(2026, 10, 16),
                                source_bytes=b"version two", row_count=10)
    store.add_inventory_facts(second, items)

    # The old snapshot still exists, with its facts, marked superseded.
    assert store.stats()["snapshots"] == 2
    assert store.stats()["live_snapshots"] == 1
    assert len(store.read_items(first)) == 10          # old facts not deleted
    assert store.latest_snapshot_id("inventory") == second


def test_a_different_as_of_date_does_not_supersede(store):
    a = store.add_snapshot(report_type="inventory", as_of=date(2026, 10, 15),
                           source_bytes=b"mon")
    b = store.add_snapshot(report_type="inventory", as_of=date(2026, 10, 16),
                           source_bytes=b"tue")
    assert store.stats()["live_snapshots"] == 2
    assert store.latest_snapshot_id("inventory") == b
    assert store.latest_snapshot_id("inventory", on_or_before=date(2026, 10, 15)) == a


def test_entity_and_location_isolate_snapshots(store):
    lhr = store.add_snapshot(report_type="inventory", as_of=date(2026, 10, 16),
                             entity_id="e1", location_id="lahore", source_bytes=b"a")
    khi = store.add_snapshot(report_type="inventory", as_of=date(2026, 10, 16),
                             entity_id="e1", location_id="karachi", source_bytes=b"b")
    assert lhr != khi
    assert store.stats()["live_snapshots"] == 2
    assert store.latest_snapshot_id("inventory", "e1", "lahore") == lhr
    assert store.latest_snapshot_id("inventory", "e1", "karachi") == khi


def test_rejected_rows_are_quarantined_not_dropped(store):
    sid = store.add_snapshot(report_type="inventory", as_of=date(2026, 10, 16),
                             source_bytes=b"x", row_count=8, rows_rejected=2,
                             status="partial")
    store.reject_row(sid, 4, "on_hand is not numeric", {"item_key": "X", "on_hand": "abc"})
    rows = store.con.execute(
        "SELECT reason FROM rejected_row WHERE snapshot_id = ?", [sid]
    ).fetchall()
    assert rows[0][0] == "on_hand is not numeric"
    snap = [s for s in store.snapshots() if s["snapshot_id"] == sid][0]
    assert snap["status"] == "partial"
    assert snap["rows_rejected"] == 2


def test_threshold_sets_are_deduplicated_but_changes_are_recorded(store):
    a = store.save_thresholds(Thresholds())
    b = store.save_thresholds(Thresholds())
    c = store.save_thresholds(Thresholds(excess_cover_months=4.0))
    assert a == b                 # identical config reuses the same id
    assert c != a
    actions = [e["action"] for e in store.audit_log()]
    assert actions.count("threshold_change") == 2


def test_metric_run_records_its_lineage(loaded):
    store, sid, hid, items, hist = loaded
    th = Thresholds()
    results = evaluate(items, classify(items, hist, th), th, as_of=date(2026, 10, 16))
    tsid = store.save_thresholds(th)
    costs = {i.item_key: i.unit_cost for i in items}
    run_id = store.save_metric_run(
        results, as_of=date(2026, 10, 16), threshold_set_id=tsid,
        snapshot_ids=[sid, hid],
        order_values={r.item_key: r.suggested_order_qty * costs[r.item_key]
                      for r in results},
    )
    runs = store.runs()
    assert runs[0]["run_id"] == run_id
    assert runs[0]["item_count"] == len(results)
    stored = store.run_results(run_id)
    assert len(stored) == len(results)

    lineage = store.con.execute(
        "SELECT snapshot_ids, threshold_set_id FROM metric_run WHERE run_id = ?",
        [run_id],
    ).fetchone()
    assert sid in lineage[0] and hid in lineage[0]
    assert lineage[1] == tsid


def test_audit_log_is_append_only_and_ordered(store):
    store.log("login", "user", "u1")
    store.log("export", "report", "r1")
    entries = store.audit_log()
    assert entries[0]["action"] == "export"     # newest first
    assert len(entries) >= 2


def test_history_lets_you_compare_two_dates(store):
    """The point of snapshots: yesterday's answer is still reproducible."""
    items = load_items(INV)
    hist = load_history(HIST)
    th = Thresholds()

    for day, on_hand_scale in [(date(2026, 10, 15), 1.0), (date(2026, 10, 16), 0.5)]:
        scaled = [
            type(i)(**{**i.__dict__, "on_hand": i.on_hand * on_hand_scale})
            for i in items
        ]
        sid = store.add_snapshot(report_type="inventory", as_of=day,
                                 source_bytes=f"day-{day}".encode())
        store.add_inventory_facts(sid, scaled)
        hid = store.add_snapshot(report_type="demand", as_of=day,
                                 source_bytes=f"hist-{day}".encode())
        store.add_demand_facts(hid, hist)

        its = store.read_items(sid)
        res = evaluate(its, classify(its, hist, th), th, as_of=day)
        store.save_metric_run(
            res, as_of=day, threshold_set_id=store.save_thresholds(th),
            snapshot_ids=[sid, hid],
            order_values={r.item_key: 0.0 for r in res},
        )

    runs = store.runs()
    assert len(runs) == 2
    # Halving stock must raise the critical count, not lower it
    newer, older = runs[0], runs[1]
    assert newer["critical"] >= older["critical"]
    assert newer["stock_value"] < older["stock_value"]
