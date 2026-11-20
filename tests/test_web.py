"""Web shell tests.

The shell must compute nothing. Every number has to come from the engine, and
the tests that matter here are the ones asserting the shell cannot quietly
change, invent, or backfill a figure.
"""

import csv
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from engine import parse, report, samples
from engine.samples import DATASETS, ORDER
from engine.schema import Thresholds
from web.app import app

client = TestClient(app)
AS_OF = samples.AS_OF.isoformat()


def _sample_files(*keys):
    return {
        k: (DATASETS[k].filename, samples.sample_csv(k), "text/csv") for k in keys
    }


def _unique(dataset_id: str, marker: str) -> str:
    """The same bytes uploaded twice are refused as a duplicate, and that check
    is global rather than scoped to one key, so a test that needs a fresh
    upload has to supply content nothing else in the suite has sent."""
    text = samples.sample_csv(dataset_id)
    return text.replace("Haematology reagent pack", f"Reagent pack {marker}")


def _unique_files(dataset_id: str, marker: str):
    return {
        dataset_id: (DATASETS[dataset_id].filename, _unique(dataset_id, marker),
                     "text/csv")
    }


# ------------------------------------------------------------------ basics


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["version"]


def test_index_declares_that_it_is_not_the_product():
    body = client.get("/").text
    assert "never leaves the machine" in body
    assert "shown over the web" in body


# -------------------------------------------------------------- navigation


def test_meta_lists_built_and_unbuilt_modules_honestly():
    modules = {
        m["id"]: m
        for d in client.get("/api/meta").json()["domains"]
        for m in d["modules"]
    }
    for built in ("dashboard", "stock", "transfers", "receivables",
                  "machines", "service", "purchasing", "cash", "forecast",
                  "pack", "history", "audit"):
        assert modules[built]["built"] is True, built
    # Payables is the only module left unbuilt, and it says why.
    assert modules["payables"]["built"] is False
    assert modules["payables"]["why"]


def test_every_unbuilt_module_says_why_rather_than_showing_fake_numbers():
    """A placeholder tile is how a demo becomes a lie you defend later."""
    for domain in client.get("/api/meta").json()["domains"]:
        for module in domain["modules"]:
            if not module["built"]:
                assert module.get("why"), module["id"]


def test_navigation_does_not_name_other_products():
    """These labels were removed deliberately: the product should stand on
    its own domains rather than advertising what it was modelled on."""
    body = client.get("/api/meta").text
    for name in ("Cin7", "Xero", "Employment Hero", "borrowed"):
        assert name not in body


# ----------------------------------------------------- the data pack loop


@pytest.mark.parametrize("dataset_id", ORDER)
def test_each_sample_file_downloads_as_an_attachment(dataset_id):
    r = client.get(f"/api/sample/{dataset_id}.csv")
    assert r.status_code == 200
    assert DATASETS[dataset_id].filename in r.headers["content-disposition"]
    header = next(csv.reader(io.StringIO(r.text)))
    assert header == DATASETS[dataset_id].header


@pytest.mark.parametrize("dataset_id", ORDER)
def test_a_dataset_can_be_fetched_by_filename_as_well_as_by_id(dataset_id):
    """The file you download is named after the filename, so that is the URL
    anyone will reach for."""
    stem = DATASETS[dataset_id].filename.replace(".csv", "")
    assert client.get(f"/api/sample/{stem}.csv").status_code == 200
    assert client.get(f"/api/template/{stem}.csv").status_code == 200


def test_an_unknown_dataset_name_is_a_404_that_lists_what_exists():
    r = client.get("/api/sample/nonsense.csv")
    assert r.status_code == 404
    assert "known" in r.json()


def test_the_zip_pack_contains_every_file_and_the_readme():
    r = client.get("/api/pack.zip")
    assert r.status_code == 200
    names = set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert names == {DATASETS[k].filename for k in ORDER} | {"README.md"}


def test_the_template_zip_is_separate_and_parses():
    r = client.get("/api/templates.zip")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    for dataset_id in ORDER:
        name = DATASETS[dataset_id].filename.replace(".csv", "_template.csv")
        records, rejected = parse.parse(dataset_id, zf.read(name).decode())
        assert rejected == []
        assert len(records) == 1


def test_downloading_the_pack_and_uploading_it_back_reproduces_the_run():
    """The whole point of the loop. If these disagree, a customer's first
    upload silently contradicts the demo they were shown."""
    sample = client.post("/api/analyse", data={"as_of": AS_OF}).json()
    uploaded = client.post(
        "/api/analyse", data={"as_of": AS_OF}, files=_sample_files(*ORDER)
    ).json()

    assert uploaded["source"] == "uploaded"
    assert sample["source"] == "sample"
    assert uploaded["headline"] == sample["headline"]
    assert uploaded["stock"]["counts"] == sample["stock"]["counts"]
    assert uploaded["group"]["stock_value"] == sample["group"]["stock_value"]
    assert uploaded["group"]["cash_trapped"] == sample["group"]["cash_trapped"]
    assert uploaded["group"]["transfer_count"] == sample["group"]["transfer_count"]


def test_the_datasets_endpoint_documents_every_column():
    payload = client.get("/api/datasets").json()
    by_id = {d["id"]: d for d in payload["datasets"]}
    for dataset_id in ORDER:
        names = [c["name"] for c in by_id[dataset_id]["columns"]]
        assert names == DATASETS[dataset_id].header
        assert all(c["note"] for c in by_id[dataset_id]["columns"])


# ------------------------------------------------------------- the numbers


def test_web_numbers_match_the_engine_exactly():
    """The shell is a window. It must not recompute anything."""
    records = {k: parse.parse(k, samples.sample_csv(k))[0] for k in ORDER}
    expected = report.build(
        items=records["inventory"],
        demand=records["demand"],
        invoices=records["receivables"],
        jobs=records["service"],
        placements=records["machines"],
        thresholds=Thresholds(),
        as_of=samples.AS_OF,
    )
    got = client.post("/api/analyse", data={"as_of": AS_OF, "persist": "0"}).json()

    assert got["headline"] == expected["headline"]
    assert got["group"]["stock_value"] == expected["group"]["stock_value"]
    assert got["stock"]["counts"] == expected["stock"]["counts"]
    assert [r["item_key"] for r in got["stock"]["rows"]] == [
        r["item_key"] for r in expected["stock"]["rows"]
    ]
    assert len(got["insights"]) == len(expected["insights"])
    for a, b in zip(got["insights"], expected["insights"]):
        assert a["title"] == b["title"]
        assert a["value"] == pytest.approx(b["value"])


def test_internal_result_objects_are_not_leaked_into_the_response():
    got = client.post("/api/analyse", data={"as_of": AS_OF}).json()
    assert "_results" not in got
    assert "_order_values" not in got


def test_thresholds_change_the_answer_and_are_echoed_back():
    loose = client.post(
        "/api/analyse", data={"as_of": AS_OF, "excess_months": "24", "persist": "0"}
    ).json()
    tight = client.post(
        "/api/analyse", data={"as_of": AS_OF, "excess_months": "1", "persist": "0"}
    ).json()
    assert loose["settings"]["excess_cover_months"] == 24
    assert tight["settings"]["excess_cover_months"] == 1
    assert tight["group"]["cash_trapped"] > loose["group"]["cash_trapped"]


def test_dso_is_absent_until_monthly_revenue_is_supplied():
    """Deriving it from the receivables alone would be circular, so the shell
    must not fill the gap with a flattering guess."""
    without = client.post("/api/analyse", data={"as_of": AS_OF, "persist": "0"}).json()
    assert without["group"]["dso_days"] is None

    with_rev = client.post(
        "/api/analyse",
        data={"as_of": AS_OF, "monthly_revenue": "42000000", "persist": "0"},
    ).json()
    assert with_rev["group"]["dso_days"] is not None


# ----------------------------------------------------------------- uploads


def test_uploaded_data_is_never_mixed_with_sample_data():
    """Backfilling the datasets you did not send would produce a dashboard
    that looks complete and is partly invented."""
    r = client.post(
        "/api/analyse", data={"as_of": AS_OF}, files=_sample_files("inventory", "demand")
    ).json()
    assert r["datasets_supplied"] == ["demand", "inventory"]
    assert set(r["datasets_missing"]) == set(ORDER) - {"inventory", "demand"}
    assert r["loaded"]["receivables"] == 0
    assert r["receivables"]["customers"] == []
    assert r["machines"]["rows"] == []


def test_rejected_rows_are_reported_with_their_line_numbers():
    bad = (
        "entity_id,location_id,item_key,on_hand,unit_cost,lead_time_days\n"
        "DX,KHI,A1,100,500,60\n"
        "DX,KHI,,5,5,5\n"
        "DX,KHI,A3,bad,5,5\n"
    )
    r = client.post(
        "/api/analyse",
        data={"as_of": AS_OF},
        files={"inventory": ("broken.csv", bad, "text/csv")},
    )
    assert r.status_code == 200
    quality = r.json()["quality"]["inventory"]
    assert quality["rows_used"] == 1
    assert {x["row"] for x in quality["rejected"]} == {3, 4}


def test_a_file_in_the_wrong_slot_is_refused_rather_than_computed_on():
    """Without this the dashboard reads every item as no-history and reports
    zero cash, which looks like an answer."""
    r = client.post(
        "/api/analyse",
        data={"as_of": AS_OF},
        files={"demand": ("oops.csv", samples.sample_csv("inventory"), "text/csv")},
    )
    assert r.status_code == 400
    assert "missing required columns" in r.json()["error"]
    assert "period" in r.json()["error"]


def test_an_empty_upload_is_refused():
    r = client.post(
        "/api/analyse",
        data={"as_of": AS_OF},
        files={"inventory": ("empty.csv", "", "text/csv")},
    )
    assert r.status_code == 400


# ------------------------------------------------------------- persistence


def test_a_run_is_persisted_and_appears_in_history():
    before = len(client.get("/api/history").json()["runs"])
    # Unique content, because duplicate detection is on bytes alone and an
    # earlier test in this module may already have filed the sample pack.
    r = client.post(
        "/api/analyse",
        data={"as_of": "2026-10-16"},
        files=_unique_files("inventory", "run-history"),
    ).json()
    assert r["run_id"]
    after = client.get("/api/history").json()
    assert len(after["runs"]) == before + 1
    assert after["runs"][0]["run_id"] == r["run_id"]


def test_a_multi_location_file_is_stored_as_one_snapshot_per_branch():
    """The snapshot key is (entity, location, report type, date). Storing a
    three-branch file as one snapshot would let Lahore's re-upload supersede
    Karachi's data."""
    r = client.post(
        "/api/analyse",
        data={"as_of": "2026-09-30"},
        files=_unique_files("inventory", "per-branch"),
    ).json()
    assert r["snapshot_ids"]
    snaps = client.get("/api/history").json()["snapshots"]
    mine = [s for s in snaps if s["snapshot_id"] in r["snapshot_ids"]]
    keys = {(s["entity_id"], s["location_id"]) for s in mine}
    assert ("DX", "KHI") in keys
    assert ("DX", "LHR") in keys
    assert ("MS", "ISB") in keys


def test_every_dataset_gets_a_snapshot_so_the_audit_trail_is_complete():
    client.post("/api/analyse", data={"as_of": "2026-08-31"})
    types = {s["report_type"] for s in client.get("/api/history").json()["snapshots"]}
    assert {"inventory", "demand", "receivables", "service", "placements"} <= types


def test_the_same_file_twice_is_detected_and_not_double_counted():
    payload = {"as_of": "2026-07-31"}
    files = _unique_files("inventory", "dupe-check")
    first = client.post("/api/analyse", data=payload, files=files).json()
    second = client.post("/api/analyse", data=payload, files=files).json()
    assert first["snapshot_ids"]
    assert second["duplicates"]
    assert second["snapshot_ids"] == []


def test_identical_bytes_are_refused_even_for_a_different_as_of_date():
    """Documenting current behaviour rather than endorsing it: the duplicate
    check is on content alone, so the same unchanged export cannot be filed
    against two different months. That is a deliberate product decision to
    revisit, not an accident."""
    files = _unique_files("inventory", "cross-date")
    first = client.post("/api/analyse", data={"as_of": "2026-05-31"}, files=files).json()
    second = client.post("/api/analyse", data={"as_of": "2026-04-30"}, files=files).json()
    assert first["snapshot_ids"]
    assert second["snapshot_ids"] == []
    assert second["duplicates"]


def test_the_audit_log_records_uploads():
    client.post("/api/analyse", data={"as_of": "2026-06-30"})
    entries = client.get("/api/audit").json()["entries"]
    assert any(e["action"] == "upload" for e in entries)


def test_persist_can_be_turned_off():
    r = client.post("/api/analyse", data={"as_of": AS_OF, "persist": "0"}).json()
    assert r["run_id"] is None
    assert r["snapshot_ids"] == []


def test_group_write_returns_the_whole_config():
    """The page keeps one copy of the configuration and repaints from it, so a
    write that answers with only its own fragment leaves the bar stale."""
    r = client.post("/api/config/group", data={"name": "Diagnostix Group"})
    assert r.status_code == 200
    body = r.json()
    assert body["group"]["name"] == "Diagnostix Group"
    assert "config" in body
    assert body["config"]["group"]["name"] == "Diagnostix Group"


def test_every_config_write_returns_the_whole_config():
    posts = [
        ("/api/config/group", {"name": "G"}),
        ("/api/config/entity", {"entity_id": "DX", "legal_name": "Diagnostix"}),
        ("/api/config/location",
         {"location_id": "KHI", "entity_id": "DX", "name": "Karachi"}),
        ("/api/config/alias",
         {"target_type": "item", "raw_value": "PIMS-2", "target_key": "PIMS"}),
    ]
    for url, data in posts:
        body = client.post(url, data=data).json()
        assert "config" in body, f"{url} answered without the configuration"


def test_the_api_says_which_rows_were_only_discovered():
    client.post("/api/analyse", data={"as_of": "2026-11-20"})
    cfg = client.get("/api/config").json()
    assert cfg["entities"], "the sample pack mentions companies"
    # Nothing was set up through a form in this request, so nothing is
    # configured: the rows exist only because a snapshot had to reference them.
    assert all("configured" in e for e in cfg["entities"])
    assert all("configured" in l for l in cfg["locations"])


def test_parallel_requests_do_not_collide_on_the_connection():
    """The page fires four requests at once on boot. One DuckDB connection is
    shared by all of them, and DuckDB refuses a second call on a busy
    connection rather than waiting, so before the store took a lock whichever
    request lost the race got a 500 and the navigation rail came up empty.

    The failure was intermittent: this hammers the endpoints concurrently so
    a regression shows up as a failure rather than as a flake in the browser.
    """
    from concurrent.futures import ThreadPoolExecutor

    paths = ["/api/meta", "/api/config", "/api/datasets", "/api/history",
             "/api/audit"] * 8

    def hit(path):
        return path, client.get(path).status_code

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(hit, paths))

    bad = [(p, c) for p, c in results if c != 200]
    assert not bad, f"{len(bad)} of {len(results)} failed: {bad[:5]}"


def test_reads_and_writes_in_parallel_do_not_collide():
    from concurrent.futures import ThreadPoolExecutor

    def work(i):
        if i % 3 == 0:
            r = client.post("/api/config/entity",
                            data={"entity_id": f"E{i}", "legal_name": f"Co {i}"})
        else:
            r = client.get("/api/meta" if i % 2 else "/api/config")
        return i, r.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(24)))

    bad = [(i, c) for i, c in results if c != 200]
    assert not bad, f"failed: {bad[:5]}"


def test_the_excel_data_model_downloads():
    r = client.get("/api/workbook.xlsx")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert "sop-insight-data-model.xlsx" in r.headers["content-disposition"]
    assert r.content[:2] == b"PK", "not a zip, so not an xlsx"
    assert len(r.content) > 50_000


def test_the_workbook_is_built_once_and_kept():
    """It is the same file every time and takes a second to build."""
    a = client.get("/api/workbook.xlsx").content
    b = client.get("/api/workbook.xlsx").content
    assert a == b


def test_a_record_without_a_branch_still_saves():
    """A trial balance belongs to a company, not a branch. Asking an account
    for a location_id raised an AttributeError that cost the whole save,
    while the answer on screen looked complete."""
    r = client.post("/api/analyse", data={"as_of": "2026-11-20"})
    assert r.status_code == 200
    body = r.json()
    assert "persist_error" not in body, body.get("persist_error")
    assert body["run_id"]
    # Either it filed snapshots or it refused them as duplicates of an
    # earlier identical upload in this process. Both are the save working;
    # the failure this guards against is the save falling over entirely.
    assert body["snapshot_ids"] or body["duplicates"]


def test_the_new_statements_reach_the_payload():
    d = client.post("/api/analyse", data={"as_of": "2026-11-20"}).json()
    assert d["finance"]["has"] and d["finance"]["ties"]
    assert d["payroll"]["has"] and d["payroll"]["headcount"] == 8
    assert d["marketing"]["has"] and d["marketing"]["open_count"] > 0
    # payroll names the law it used, so a stale table cannot hide
    assert "Finance Act 2026" in d["payroll"]["table_source"]


def test_finance_is_company_level_and_says_so_under_a_branch():
    d = client.post("/api/analyse",
                    data={"as_of": "2026-11-20", "entity": "DX",
                          "location": "KHI"}).json()
    assert "finance" in d["scope"]["group_level"]
    company = client.post("/api/analyse",
                          data={"as_of": "2026-11-20", "entity": "DX"}).json()
    assert "finance" not in company["scope"]["group_level"]


def test_changing_a_threshold_and_recalculating_is_still_a_run():
    """No new files are filed -- the content is identical -- but it is a
    different answer, and a history that drops it cannot explain why a
    figure moved."""
    first = client.post("/api/analyse", data={"as_of": "2026-11-20"}).json()
    again = client.post("/api/analyse",
                        data={"as_of": "2026-11-20", "cash_buffer": "5000000"}).json()
    assert again["run_id"]
    assert again["run_id"] != first["run_id"]
    runs = client.get("/api/history").json()["runs"]
    assert len(runs) >= 2
