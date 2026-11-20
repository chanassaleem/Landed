"""The download-edit-upload loop.

The contract under test: anything the product hands out, the product can read
back. If a template or a sample file stops parsing, the loop is broken and a
customer's first experience of the product is a rejected upload.
"""

import csv
import io

import pytest

from engine import parse as parser
from engine import samples
from engine.samples import DATASETS, ORDER


@pytest.mark.parametrize("dataset_id", ORDER)
def test_every_sample_file_parses_with_no_rejections(dataset_id):
    """The loop's core promise: download, upload, no complaints."""
    records, rejected = parser.parse(dataset_id, samples.sample_csv(dataset_id))
    assert rejected == []
    assert len(records) > 0


@pytest.mark.parametrize("dataset_id", ORDER)
def test_every_blank_template_parses_with_no_rejections(dataset_id):
    """The example row in a template must itself be valid, or the first thing
    a customer does after downloading is hit an error."""
    records, rejected = parser.parse(dataset_id, samples.template_csv(dataset_id))
    assert rejected == []
    assert len(records) == 1


@pytest.mark.parametrize("dataset_id", ORDER)
def test_no_required_column_is_missing_from_the_registry_output(dataset_id):
    assert parser.missing_columns(dataset_id, samples.sample_csv(dataset_id)) == []


@pytest.mark.parametrize("dataset_id", ORDER)
def test_header_matches_the_registry_exactly(dataset_id):
    """The header is generated from the registry, so a column renamed in one
    place cannot survive here."""
    text = samples.sample_csv(dataset_id)
    header = next(csv.reader(io.StringIO(text)))
    assert header == DATASETS[dataset_id].header


@pytest.mark.parametrize("dataset_id", ORDER)
def test_every_registry_column_has_an_example_and_a_note(dataset_id):
    for col in DATASETS[dataset_id].columns:
        # A column that is meant to be left blank -- a leaving date for
        # somebody still employed -- has no example to give, and inventing
        # one would put a value in the template that the parser then has to
        # reject. Its note has to say that it is blank on purpose.
        if col.example == "":
            assert "blank" in col.note.lower(), (
                f"{dataset_id}.{col.name} has no example and does not say "
                f"it is meant to be blank"
            )
        assert col.note != "", f"{dataset_id}.{col.name} has no note"


def test_generation_is_deterministic():
    """Two people comparing results must be looking at the same numbers."""
    for dataset_id in ORDER:
        assert samples.sample_csv(dataset_id) == samples.sample_csv(dataset_id)


def test_the_pack_contains_every_dataset_and_a_readme():
    pack = samples.pack()
    for dataset_id in ORDER:
        assert DATASETS[dataset_id].filename in pack
    assert "README.md" in pack


def test_the_template_pack_is_named_separately_from_the_sample_pack():
    """Downloading both must not overwrite one with the other."""
    assert set(samples.pack()) & set(samples.pack(templates=True)) == {"README.md"}


def test_the_readme_documents_every_column():
    notes = samples.notes_md()
    for dataset_id in ORDER:
        for col in DATASETS[dataset_id].columns:
            assert f"`{col.name}`" in notes


# ----------------------------------------------- what the data must contain
#
# The pack exists to exercise the engine, so these assert the scenarios are
# actually present. If a tuning change removes one, the demo quietly stops
# showing the insight it was built to show.


def test_the_pack_spans_several_entities_and_locations():
    records, _ = parser.parse("inventory", samples.sample_csv("inventory"))
    assert len({r.entity_id for r in records}) >= 2
    assert len({r.location_id for r in records}) >= 3


def test_the_pack_contains_an_intercompany_invoice():
    records, _ = parser.parse("receivables", samples.sample_csv("receivables"))
    assert any(r.is_intercompany for r in records)


def test_the_pack_contains_a_customer_owing_money_in_two_entities():
    """Group exposure is invisible without one."""
    records, _ = parser.parse("receivables", samples.sample_csv("receivables"))
    by_customer = {}
    for r in records:
        if r.is_intercompany:
            continue
        by_customer.setdefault(r.customer_key, set()).add(r.entity_id)
    assert any(len(ents) > 1 for ents in by_customer.values())


def test_the_pack_contains_an_expiring_batch():
    records, _ = parser.parse("inventory", samples.sample_csv("inventory"))
    assert any(r.expiry_date is not None for r in records)


def test_the_pack_contains_both_clinical_and_ordinary_items():
    records, _ = parser.parse("inventory", samples.sample_csv("inventory"))
    flags = {r.is_clinical_critical for r in records}
    assert flags == {True, False}


def test_the_pack_contains_unbilled_labour_and_parts_fitted_free():
    records, _ = parser.parse("service", samples.sample_csv("service"))
    billable = [j for j in records if not j.is_warranty]
    assert any(j.labour_hours > j.labour_hours_billed for j in billable)
    assert any(j.parts_cost > j.parts_charged for j in billable)


def test_the_pack_contains_warranty_and_repeat_visits():
    records, _ = parser.parse("service", samples.sample_csv("service"))
    assert any(j.is_warranty for j in records)
    assert any(j.is_repeat_visit for j in records)
    assert any(j.closed_date is None for j in records)


def test_the_pack_contains_a_placement_running_well_below_forecast():
    records, _ = parser.parse("machines", samples.sample_csv("machines"))
    assert any(
        m.expected_monthly_tests > 0
        and m.actual_monthly_tests / m.expected_monthly_tests < 0.4
        for m in records
    )


def test_demand_history_is_twelve_months_and_includes_zero_months():
    """A zero month is information. Classification is wrong without it."""
    records, _ = parser.parse("demand", samples.sample_csv("demand"))
    assert len({r.period for r in records}) == 12
    assert any(r.quantity == 0 for r in records)


def test_every_demand_item_exists_in_the_inventory_file():
    """A mismatch here is the single most common real-world upload fault, so
    the pack must not ship with one."""
    inv, _ = parser.parse("inventory", samples.sample_csv("inventory"))
    dem, _ = parser.parse("demand", samples.sample_csv("demand"))
    assert {r.item_key for r in dem} <= {r.item_key for r in inv}


def test_every_service_job_points_at_a_real_machine():
    jobs, _ = parser.parse("service", samples.sample_csv("service"))
    machines, _ = parser.parse("machines", samples.sample_csv("machines"))
    assert {j.machine_key for j in jobs} <= {m.machine_key for m in machines}
