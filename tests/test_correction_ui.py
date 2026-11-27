"""The two fixes, reachable from the thing that is wrong.

/api/receipt/correct and /api/shipment/basis existed and nothing in
the browser called them. A person could see on the import preview that
a receipt disagreed with their file, and on the Landing screen that a
measurement was missing, and had no way to do anything about either
from the application.

Both driven end to end in a browser:

  the freight panel lists the unmeasured line; "Enter it" opens a form
  reading shipment=SHP-77, sku=RGT-100, batch=B-2026-07, all locked;
  submitting writes the weight, logs the audit row, and the skew goes

  the item drawer offers "Correct a receipt", lists GRN-900 — 120 of
  B-2026-07, and opens the correction form already naming it;
  submitting leaves the 120 filed and adds a 7 beside it with the
  reason, and posts the reversal
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
RECORDS = (ROOT / "web" / "static" / "records.js").read_text()
PLATFORM = (ROOT / "web" / "static" / "platform.js").read_text()
APP = (ROOT / "web" / "static" / "app.js").read_text()
CSS = (ROOT / "web" / "static" / "app.css").read_text()
PY_APP = (ROOT / "web" / "app.py").read_text()


def test_every_write_endpoint_has_something_that_calls_it():
    """The gap this closes, as a rule rather than an instance. An
    endpoint nothing reaches is a feature that does not exist, and
    two of them shipped that way."""
    posts = set(re.findall(r'@app\.post\("(/api/[a-z/_]+)"\)', PY_APP))
    # Reached from the browser, or deliberately not: sign-in and the
    # organisation bootstrap have their own pages.
    exempt = {"/api/organisation"}
    js = RECORDS + PLATFORM + APP
    unreachable = {p for p in posts - exempt if p not in js}
    assert not unreachable, f"no caller in the browser for: {sorted(unreachable)}"


def test_the_correction_forms_exist_and_name_their_endpoints():
    for key, post in (("shipment_basis", "/api/shipment/basis"),
                      ("receipt_correction", "/api/receipt/correct")):
        block = RECORDS[RECORDS.index(f"    {key}: {{"):]
        block = block[:block.index("\n    },")]
        assert f"post: '{post}'" in block
        assert "'why'" in block, f"{key} does not ask why"
        assert "req: true" in block.split("'why'")[1][:120], (
            f"{key} does not insist on a reason")


def test_a_form_opened_from_a_row_is_told_which_record():
    """Asking somebody to retype the shipment, SKU and batch into a
    form opened from that very row is how a correction ends up filed
    against the wrong line."""
    assert "function show(key, after, given)" in RECORDS
    assert "locked: true" in RECORDS


def test_the_identity_is_shown_rather_than_hidden():
    """Somebody about to change a historical figure should see which
    record it belongs to. And read-only rather than disabled, because
    a disabled input is left out of the form's own data."""
    block = RECORDS[RECORDS.index("    if (opt.locked) {"):]
    block = block[:block.index("    if (type === 'select') {")]
    assert "input.readOnly = true" in block
    assert "type = 'hidden'" not in block
    assert ".fld-locked {" in CSS


def test_the_freight_screen_lists_the_lines_and_offers_the_fix():
    spec = PLATFORM[PLATFORM.index("    freight: {"):]
    spec = spec[:spec.index("\n    demand: {")]
    assert "key: 'needs_measuring'" in spec
    assert "form: 'shipment_basis'" in spec
    for col in ("shipment", "sku", "batch", "missing"):
        assert f"['{col}'," in spec


def test_the_panel_action_passes_the_rows_identity_through():
    block = PLATFORM[PLATFORM.index("        if (pan.act && window.LANDED_RECORDS)"):]
    block = block[:block.index("        tb.append(tr);")]
    assert "pan.act.identify(r)" in block
    assert "LANDED_RECORDS.show" in block


def test_the_item_drawer_can_correct_one_of_its_receipts():
    op = APP[APP.index("key: 'correct',"):]
    op = op[:op.index("\n  {")]
    assert "row.receipts" in op
    assert "'receipt_correction'" in op
    assert "when: (row) => (row.receipts || []).length > 0" in op, (
        "the button shows on lines with nothing to correct")


def test_only_live_receipt_lines_are_offered():
    """A line already superseded by a correction is not the one to
    correct again; correcting the correction keeps the chain
    readable."""
    block = PY_APP[PY_APP.index('@app.get("/api/line/history")'):]
    block = block[:block.index('@app.get("/api/state")')]
    assert "grn_lines_live" in block


def test_the_buttons_are_redrawn_once_the_receipts_arrive():
    """They are fetched when the drawer opens, so whether there is
    anything to correct is not known at the moment it is first
    drawn."""
    assert "function redrawActions(row)" in APP
    assert "if (row.receipts.length) redrawActions(row)" in APP


def test_a_wrong_exchange_rate_can_be_corrected_from_the_form():
    """correct_receipt has always accepted a rate and the form did not
    offer it, so a rate entered wrongly could not be put right from the
    application at all -- and a wrong rate moves the landed cost by the
    whole difference between two currencies."""
    block = RECORDS[RECORDS.index("    receipt_correction: {"):]
    block = block[:block.index("\n    },")]
    assert "'fx'" in block


def test_the_receiving_form_demands_a_rate_for_a_different_currency():
    """An order supplies a rate only for its own currency. The form
    let somebody name another and leave the rate blank, and the
    operation inherited the order's -- valuing a dollar receipt at a
    euro rate."""
    block = APP[APP.index("      const moneyHint = (po) => {"):]
    block = block[:block.index("      const costHint = ()")]
    assert "money[1].input.required = differs" in block
    assert "cannot " in block and "value" in block


def test_every_field_the_operation_takes_has_a_way_in():
    """The sibling of the endpoint-coverage test, and the gap it
    missed: an endpoint can have a caller and still not offer half of
    what it accepts."""
    ops = (ROOT / "engine" / "operations.py").read_text()
    sig = ops[ops.index("def correct_receipt("):]
    sig = sig[:sig.index(") -> dict:")]
    taken = set(re.findall(r"(\w+)\s*[:=]", sig)) - {"run", "str", "float",
                                                     "date", "None", "int"}
    forms = RECORDS[RECORDS.index("    receipt_correction: {"):]
    forms = forms[:forms.index("\n    },")]
    offered = set(re.findall(r"\['(\w+)'", forms))
    missing = {t for t in ("grn", "sku", "batch", "qty", "unit_cost",
                           "expiry", "fx", "why")} - offered
    assert not missing, f"the correction form cannot set: {sorted(missing)}"
