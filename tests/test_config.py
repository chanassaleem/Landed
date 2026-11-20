"""Master data and alias mapping.

The schema has carried entity, location, customer and alias from the start and
nothing ever wrote to them except the stubs an upload creates. These are the
tests for making a company and a branch into objects somebody can configure,
and for turning a sanity finding about one customer under two codes into a
decision that moves the figures.
"""

from datetime import date

import pytest

from engine import alias
from engine.db.store import Store
from engine.modules.receivables import evaluate
from engine.schema import ItemSnapshot, ReceivableInvoice, Thresholds


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def inv(no, cust, entity, amount, **kw):
    return ReceivableInvoice(
        invoice_no=no, customer_key=cust, customer_name=kw.pop("name", cust),
        invoice_date=date(2026, 1, 1), due_date=date(2026, 4, 1),
        amount=float(amount), entity_id=entity, location_id="KHI",
        is_public_sector=True, **kw,
    )


# ------------------------------------------------------------------ group


def test_a_fresh_install_has_an_unnamed_group(store):
    assert store.group()["name"] == ""


def test_the_group_can_be_named_and_is_audited(store):
    store.save_group("Diagnostix Group", "PK", "PKR")
    assert store.group()["name"] == "Diagnostix Group"
    assert any(e["action"] == "configure" for e in store.audit_log(10))


def test_naming_the_group_again_replaces_rather_than_duplicates(store):
    store.save_group("First")
    store.save_group("Second")
    assert store.group()["name"] == "Second"


# --------------------------------------------------------------- companies


def test_a_company_is_an_object_not_a_string_in_a_csv_row(store):
    store.save_entity("DX", "Diagnostix (Pvt) Ltd", "1234567-8", True)
    row = store.entities()[0]
    assert row["entity_id"] == "DX"
    assert row["legal_name"] == "Diagnostix (Pvt) Ltd"
    assert row["ntn"] == "1234567-8"
    assert row["is_separate_company"] is True
    assert row["branches"] == 0


def test_saving_a_company_twice_updates_it(store):
    store.save_entity("DX", "Old name")
    store.save_entity("DX", "New name")
    assert len(store.entities()) == 1
    assert store.entities()[0]["legal_name"] == "New name"


def test_a_company_needs_a_code_and_a_name(store):
    with pytest.raises(ValueError):
        store.save_entity("", "Nameless")
    with pytest.raises(ValueError):
        store.save_entity("DX", "   ")


def test_separate_company_is_recorded_because_it_decides_consolidation(store):
    """Separate companies need inter-company elimination; branches of one
    company do not."""
    store.save_entity("DX", "Diagnostix", is_separate_company=True)
    store.save_entity("BR", "A division", is_separate_company=False)
    flags = {e["entity_id"]: e["is_separate_company"] for e in store.entities()}
    assert flags == {"DX": True, "BR": False}


# ---------------------------------------------------------------- branches


def test_a_branch_belongs_to_a_company(store):
    store.save_entity("DX", "Diagnostix")
    store.save_location("KHI", "DX", "Karachi warehouse", "Karachi", True)
    row = store.locations()[0]
    assert row["location_id"] == "KHI"
    assert row["entity_id"] == "DX"
    assert row["entity_name"] == "Diagnostix"
    assert row["city"] == "Karachi"


def test_a_branch_cannot_belong_to_a_company_that_does_not_exist(store):
    with pytest.raises(ValueError, match="no company"):
        store.save_location("KHI", "GHOST", "Nowhere")


def test_the_company_branch_count_follows_its_branches(store):
    store.save_entity("DX", "Diagnostix")
    store.save_location("KHI", "DX", "Karachi")
    store.save_location("LHR", "DX", "Lahore")
    assert store.entities()[0]["branches"] == 2


def test_a_branch_can_be_moved_between_companies(store):
    store.save_entity("DX", "Diagnostix")
    store.save_entity("MS", "MedSupply")
    store.save_location("KHI", "DX", "Karachi")
    store.save_location("KHI", "MS", "Karachi")
    assert store.locations()[0]["entity_id"] == "MS"


def test_an_unused_branch_can_be_removed(store):
    store.save_entity("DX", "Diagnostix")
    store.save_location("KHI", "DX", "Karachi")
    assert store.delete_location("KHI") is True
    assert store.locations() == []


def test_a_branch_with_snapshots_cannot_be_removed(store):
    """It is part of the audit trail. Removing it would break the promise
    that a past dashboard can be explained."""
    store.save_entity("DX", "Diagnostix")
    store.save_location("KHI", "DX", "Karachi")
    store.add_snapshot(report_type="inventory", as_of=date(2026, 11, 20),
                       entity_id="DX", location_id="KHI", row_count=1)
    with pytest.raises(ValueError, match="audit trail"):
        store.delete_location("KHI")


# ----------------------------------------------------------------- aliases


def test_an_alias_maps_one_spelling_onto_one_key(store):
    store.save_alias("customer", "PIMS-2", "PIMS")
    assert store.alias_map("customer") == {"PIMS-2": "PIMS"}


def test_remapping_the_same_spelling_replaces_the_old_mapping(store):
    store.save_alias("customer", "PIMS-2", "PIMS")
    store.save_alias("customer", "PIMS-2", "OTHER")
    assert store.alias_map("customer") == {"PIMS-2": "OTHER"}
    assert len(store.aliases("customer")) == 1


def test_a_value_cannot_be_an_alias_of_itself(store):
    with pytest.raises(ValueError, match="alias of itself"):
        store.save_alias("customer", "PIMS", "PIMS")


def test_only_the_three_known_target_types_are_accepted(store):
    with pytest.raises(ValueError):
        store.save_alias("supplier", "A", "B")
    for kind in ("item", "customer", "machine"):
        store.save_alias(kind, f"raw-{kind}", f"key-{kind}")


def test_alias_maps_are_separate_per_type(store):
    store.save_alias("customer", "X", "Y")
    store.save_alias("item", "X", "Z")
    assert store.alias_map("customer") == {"X": "Y"}
    assert store.alias_map("item") == {"X": "Z"}


def test_an_alias_can_be_removed(store):
    made = store.save_alias("customer", "PIMS-2", "PIMS")
    store.delete_alias(made["alias_id"])
    assert store.alias_map("customer") == {}


def test_mapping_is_audited_with_both_sides(store):
    store.save_alias("customer", "PIMS-2", "PIMS")
    entry = next(e for e in store.audit_log(10) if e["action"] == "map_alias")
    detail = entry["detail_json"]
    assert "PIMS-2" in detail and "PIMS" in detail


# -------------------------------------------- applying aliases to records


def test_applying_an_alias_merges_a_split_debtor():
    """The exact fault sanity reports: one hospital under two codes reads as
    two manageable balances instead of one large one."""
    rows = [inv("I1", "PIMS", "DX", 5_000_000), inv("I2", "PIMS-2", "MS", 9_000_000)]

    before = evaluate(rows, Thresholds(), date(2026, 11, 20))
    assert len(before.customers) == 2
    assert before.customers[0].outstanding == 9_000_000
    assert before.customers[0].entities == ("MS",)

    merged = alias.apply(invoices=rows, customer_map={"PIMS-2": "PIMS"})
    after = evaluate(merged["invoices"], Thresholds(), date(2026, 11, 20))
    assert len(after.customers) == 1
    assert after.customers[0].outstanding == 14_000_000
    assert after.customers[0].entities == ("DX", "MS")


def test_applying_reports_what_it_moved_rather_than_doing_it_invisibly():
    rows = [inv("I1", "PIMS", "DX", 1), inv("I2", "PIMS-2", "MS", 1)]
    out = alias.apply(invoices=rows, customer_map={"PIMS-2": "PIMS"})
    assert out["applied"]["customer_rows"] == 1
    assert out["applied"]["total"] == 1


def test_an_item_alias_moves_both_stock_and_demand():
    from engine.schema import DemandPeriod
    items = [ItemSnapshot("OLD-1", "Pack", "Reagent", 10, 0, 500, 60, 10)]
    demand = [DemandPeriod("OLD-1", "2026-10", 5)]
    out = alias.apply(items=items, demand=demand, item_map={"OLD-1": "NEW-1"})
    assert out["items"][0].item_key == "NEW-1"
    assert out["demand"][0].item_key == "NEW-1"
    assert out["applied"]["item_rows"] == 2


def test_a_machine_alias_moves_both_jobs_and_placements():
    from engine.schema import MachinePlacement, ServiceJob
    jobs = [ServiceJob("J1", "OLD-M", date(2026, 10, 1))]
    placements = [MachinePlacement("OLD-M", "A", "C", "C", date(2025, 1, 1), 1, 12)]
    out = alias.apply(jobs=jobs, placements=placements,
                      machine_map={"OLD-M": "NEW-M"})
    assert out["jobs"][0].machine_key == "NEW-M"
    assert out["placements"][0].machine_key == "NEW-M"


def test_an_empty_mapping_changes_nothing():
    rows = [inv("I1", "PIMS", "DX", 1)]
    out = alias.apply(invoices=rows)
    assert out["invoices"][0].customer_key == "PIMS"
    assert out["applied"]["total"] == 0


def test_the_original_records_are_not_mutated():
    """The files are immutable snapshots of what the customer exported. A
    mapping is a decision about how to read them, not an edit to them."""
    rows = [inv("I1", "PIMS-2", "MS", 1)]
    alias.apply(invoices=rows, customer_map={"PIMS-2": "PIMS"})
    assert rows[0].customer_key == "PIMS-2"


# ---------------------------------------------------------------------------
# Discovery is not configuration
#
# A snapshot has to reference an entity and a location, so uploading a file
# creates those rows whether or not anybody has set them up. Before the
# `configured` flag the Settings screen read those rows back as companies the
# user had configured, complete with an Edit button and a legal name that was
# really just the code. A guess has to stay visibly a guess.
# ---------------------------------------------------------------------------

def test_upload_configures_nothing(tmp_path):
    s = Store(str(tmp_path / "d.duckdb"))
    s.ensure_location("KHI", "DX", "Karachi")
    assert [e["configured"] for e in s.entities()] == [False]
    assert [l["configured"] for l in s.locations()] == [False]
    s.close()


def test_saving_through_the_form_configures(tmp_path):
    s = Store(str(tmp_path / "d.duckdb"))
    s.ensure_location("KHI", "DX", "Karachi")
    s.save_entity("DX", "Diagnostix (Pvt) Ltd", actor="anas")
    assert s.entities()[0]["configured"] is True
    assert s.entities()[0]["legal_name"] == "Diagnostix (Pvt) Ltd"
    # the branch is untouched by naming its company
    assert s.locations()[0]["configured"] is False
    s.save_location("KHI", "DX", "Karachi warehouse", actor="anas")
    assert s.locations()[0]["configured"] is True
    s.close()


def test_discovery_does_not_overwrite_configuration(tmp_path):
    """A later upload mentioning the same code must not undo the setup."""
    s = Store(str(tmp_path / "d.duckdb"))
    s.save_entity("DX", "Diagnostix (Pvt) Ltd", ntn="1234567-8", actor="anas")
    s.ensure_entity("DX", "DX")
    e = s.entities()[0]
    assert e["configured"] is True
    assert e["legal_name"] == "Diagnostix (Pvt) Ltd"
    assert e["ntn"] == "1234567-8"
    s.close()


def test_configured_rows_sort_first(tmp_path):
    s = Store(str(tmp_path / "d.duckdb"))
    s.ensure_entity("AA")
    s.ensure_entity("ZZ")
    s.save_entity("ZZ", "Zed Medical", actor="anas")
    assert [e["entity_id"] for e in s.entities()] == ["ZZ", "AA"]
    s.close()


def test_a_database_from_before_the_flag_migrates(tmp_path):
    """CREATE TABLE IF NOT EXISTS leaves an older file on its old columns."""
    import duckdb
    path = str(tmp_path / "old.duckdb")
    con = duckdb.connect(path)
    con.execute(
        "CREATE TABLE entity (entity_id VARCHAR PRIMARY KEY, legal_name VARCHAR "
        "NOT NULL, ntn VARCHAR, is_separate_company BOOLEAN NOT NULL DEFAULT TRUE, "
        "base_currency VARCHAR NOT NULL DEFAULT 'PKR', created_at TIMESTAMP NOT NULL)"
    )
    con.execute(
        "CREATE TABLE location (location_id VARCHAR PRIMARY KEY, entity_id VARCHAR "
        "NOT NULL, name VARCHAR NOT NULL, city VARCHAR, "
        "is_warehouse BOOLEAN NOT NULL DEFAULT TRUE)"
    )
    con.execute("INSERT INTO entity VALUES ('DX','Diagnostix',NULL,TRUE,'PKR',now())")
    con.execute("INSERT INTO location VALUES ('KHI','DX','Karachi',NULL,TRUE)")
    con.commit()
    con.close()

    s = Store(path)
    # Already there before the distinction existed, so read as configured:
    # it may well have been set up by hand and nothing is lost by listing it.
    assert s.entities()[0]["configured"] is True
    assert s.locations()[0]["configured"] is True
    s.close()

    # and opening it again must not try to add the column twice
    s2 = Store(path)
    assert len(s2.entities()) == 1
    s2.close()
