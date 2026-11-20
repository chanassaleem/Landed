"""The Excel data model.

The workbook is the field specification, a place to keep the figures until the
database is wired up, and a sanity check before upload. All three only hold if
what comes out of it is exactly what the importer reads, so the test that
matters is the round trip: export every sheet, import it, and the engine must
produce the same answer it produces from the sample pack.

The rest of these exist because of mistakes made while building it. A formula
can evaluate cleanly and still be wrong: the ageing bucket read the invoice
amount instead of its age and called a 93-day invoice "180+", and the Checks
sheet counted its own headers, so every check read exactly two on a clean
workbook. Neither showed up as an error anywhere.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import parse, report, samples          # noqa: E402
from engine.schema import Thresholds               # noqa: E402
from tools.build_workbook import (                 # noqa: E402
    CALC, GREY_START, ROW_KEY, main, sheet_to_csv,
)

openpyxl = pytest.importorskip("openpyxl")

from engine.workbook import SHEETS as TITLES              # noqa: E402
AS_OF = date(2026, 11, 20)


@pytest.fixture(scope="module")
def book(tmp_path_factory):
    out = tmp_path_factory.mktemp("wb") / "model.xlsx"
    main(str(out))
    return str(out)


@pytest.fixture(scope="module")
def wb(book):
    return openpyxl.load_workbook(book)


# ------------------------------------------------------------------ shape

# The order is deliberate and is part of the deliverable: what the workbook
# is, then what it says, then what you change, then what you fill in, then
# the worked-out half, then the audit. Someone opening it for the first time
# should be able to go left to right.
READ_FIRST = ["Read me", "Summary", "Settings", "Companies", "Branches"]
DATA_SHEETS = [
    "Inventory", "Demand", "Receivables", "Service", "Machines",
    "Commitments", "Trial balance", "Payroll", "Pipeline", "Marketing spend",
]
WORKED_OUT = [
    "Profit and loss", "Balance sheet", "Cash flow", "Stock policy",
    "Payroll calculation", "Pipeline calculation", "Salary tax slabs",
]
AUDIT = ["Checks", "Lists"]


def test_every_sheet_is_there(wb):
    assert wb.sheetnames == READ_FIRST + DATA_SHEETS + WORKED_OUT + AUDIT


def test_there_is_one_data_sheet_per_dataset_the_parser_knows(wb):
    """The white half of the workbook IS the importer's surface.

    A dataset the product can import with no sheet to enter it on makes the
    workbook an incomplete model, which is the one thing it must not be.
    """
    from engine import samples
    from engine.workbook import SHEETS

    assert len(DATA_SHEETS) == len(samples.ORDER)
    for key in samples.ORDER:
        assert SHEETS[key] in wb.sheetnames, key


def test_every_calculation_sheet_is_worked_out_not_entered(wb):
    """Nothing on the worked-out sheets is typed in.

    If any of these held a constant it would be a number that stopped
    moving when its inputs did, which is the failure the grey half exists
    to prevent.
    """
    for name in WORKED_OUT:
        ws = wb[name]
        typed = []
        for row in ws.iter_rows(min_row=4):
            for c in row:
                if c.value is None or isinstance(c.value, str):
                    continue
                typed.append(f"{name}!{c.coordinate}={c.value!r}")
        # The tax slab table is the one exception: the slabs themselves are
        # the statute, and they are entered, dated and sourced on the sheet.
        if name == "Salary tax slabs":
            continue
        assert not typed, typed[:10]


def test_it_opens_on_the_instructions(wb):
    assert wb.sheetnames[0] == "Read me"
    text = " ".join(str(c.value) for row in wb["Read me"].iter_rows()
                    for c in row if c.value)
    # The legend is the whole convention, so it has to be stated.
    assert "White cells are yours to fill in" in text
    assert "Grey cells are worked out for you" in text


def test_the_read_me_names_every_sheet_in_the_workbook(wb):
    """An out-of-date contents page is worse than none.

    The Read me is the first thing opened and the only place that says what
    each sheet is for. A sheet added without a line here is a sheet nobody
    is told about.
    """
    text = " ".join(str(c.value) for row in wb["Read me"].iter_rows()
                    for c in row if c.value)
    for name in wb.sheetnames:
        if name == "Read me":
            continue
        assert name in text, f"the Read me does not mention {name}"


def test_the_read_me_is_honest_about_what_is_not_in_the_workbook(wb):
    """The three things a spreadsheet cannot do faithfully.

    The workbook now carries the product's arithmetic, which makes the
    remaining gaps easy to forget and important to state: someone who
    believes the model is complete will trust a cash answer it cannot give.
    """
    text = " ".join(str(c.value) for row in wb["Read me"].iter_rows()
                    for c in row if c.value).lower()
    assert "only in the product" in text
    for missing in ("forecast", "cash view", "sequencing"):
        assert missing in text, missing


def test_the_white_columns_are_exactly_what_the_importer_reads(wb):
    for key, title in TITLES.items():
        spec = [c.name for c in samples.DATASETS[key].columns]
        got = [c.value for c in wb[title][3]
               if c.value and not str(c.value).startswith(("calc_", "check_"))]
        assert got == spec, f"{title}: headers drifted from the template"


def test_the_grey_columns_come_after_the_white_ones(wb):
    """So the whole sheet saves as CSV and still imports."""
    for key, title in TITLES.items():
        white = max(c.column for c in wb[title][3]
                    if c.value and not str(c.value).startswith(("calc_", "check_")))
        grey = [c.column for c in wb[title][3]
                if c.value and str(c.value).startswith(("calc_", "check_"))]
        assert grey, f"{title} has no worked-out columns"
        assert min(grey) > white, title
        assert min(grey) == GREY_START


def test_colour_carries_one_meaning(wb):
    """White means type here; grey means worked out for you. Nothing else."""
    for title in TITLES.values():
        ws = wb[title]
        for cell in ws[3]:
            if not cell.value:
                continue
            grey_head = str(cell.value).startswith(("calc_", "check_"))
            body = ws.cell(row=4, column=cell.column)
            fill = (body.fill.fgColor.rgb or "").upper()
            if grey_head:
                assert fill == "FFEFEFEF", f"{title}.{cell.value} is not grey"
                assert str(body.value).startswith("="), \
                    f"{title}.{cell.value} is grey but holds no formula"
            else:
                assert fill == "FFFFFFFF", f"{title}.{cell.value} is not white"
                assert not str(body.value or "").startswith("="), \
                    f"{title}.{cell.value} is white but holds a formula"


def test_every_field_says_what_it_is(wb):
    """The note row above the header is the field definition."""
    for key, title in TITLES.items():
        ws = wb[title]
        for cell in ws[3]:
            if not cell.value:
                continue
            note = ws.cell(row=2, column=cell.column).value
            assert note and len(str(note)) >= 10, f"{title}.{cell.value} has no note"


def test_required_fields_say_so(wb):
    for key, title in TITLES.items():
        ws = wb[title]
        required = {c.name for c in samples.DATASETS[key].columns if c.required}
        for cell in ws[3]:
            if cell.value in required:
                note = str(ws.cell(row=2, column=cell.column).value)
                assert note.startswith("Required."), f"{title}.{cell.value}"


def test_there_are_rows_to_fill_in(wb):
    """It is a form as well as an example, so it needs blank rows."""
    ws = wb["Inventory"]
    assert ws.max_row > 100
    key = [c.column for c in ws[3] if c.value == ROW_KEY["inventory"]][0]
    assert ws.cell(row=ws.max_row, column=key).value is None


def test_codes_are_chosen_from_a_list_not_typed(wb):
    """A typed branch code that does not exist is the commonest bad export."""
    for title in ("Inventory", "Receivables", "Machines"):
        ranges = [str(dv.sqref) for dv in wb[title].data_validations.dataValidation]
        assert ranges, f"{title} has no dropdowns"


# ------------------------------------------------------------- round trip

def test_every_sheet_imports_with_nothing_rejected(book):
    for key, title in TITLES.items():
        text = sheet_to_csv(book, title)
        assert parse.missing_columns(key, text) == [], key
        records, rejected = parse.parse(key, text)
        assert not rejected, f"{key}: {rejected[:2]}"
        assert records, key


def test_the_row_counts_match_the_sample_pack(book):
    for key, title in TITLES.items():
        from_book, _ = parse.parse(key, sheet_to_csv(book, title))
        from_pack, _ = parse.parse(key, samples.sample_csv(key))
        assert len(from_book) == len(from_pack), key


def test_the_engine_gives_the_same_answer_from_the_workbook(book):
    """The point of the whole thing. If the workbook and the sample pack
    disagree, one of them is lying about what the product reads."""
    def run(texts):
        r = {k: parse.parse(k, t)[0] for k, t in texts.items()}
        return report.build(
            items=r["inventory"], demand=r["demand"], invoices=r["receivables"],
            jobs=r["service"], placements=r["machines"],
            commitments=r["commitments"], accounts=r["trial_balance"],
            employees=r["employees"], opportunities=r["opportunities"],
            marketing_spend=r["marketing_spend"],
            thresholds=Thresholds(), as_of=AS_OF)

    pack = run({k: samples.sample_csv(k) for k in samples.ORDER})
    book_ = run({k: sheet_to_csv(book, TITLES[k]) for k in samples.ORDER})

    assert book_["headline"] == pack["headline"]
    assert book_["receivables"]["outstanding"] == pack["receivables"]["outstanding"]
    assert book_["receivables"]["buckets"] == pack["receivables"]["buckets"]
    assert book_["cash"]["weeks"] == pack["cash"]["weeks"]
    assert len(book_["stock"]["rows"]) == len(pack["stock"]["rows"])
    assert len(book_["insights"]) == len(pack["insights"])
    # and the statements the workbook produces are the same ones
    assert book_["finance"]["pnl"] == pack["finance"]["pnl"]
    assert book_["payroll"]["employer_cost_monthly"] == \
        pack["payroll"]["employer_cost_monthly"]


def test_a_sheet_exports_only_its_white_columns(book):
    text = sheet_to_csv(book, "Inventory")
    head = next(csv.reader(io.StringIO(text)))
    assert not any(h.startswith(("calc_", "check_")) for h in head)


def test_dates_go_out_in_the_format_the_parser_wants(book):
    rows = list(csv.DictReader(io.StringIO(sheet_to_csv(book, "Receivables"))))
    assert rows[0]["due_date"].count("-") == 2
    assert len(rows[0]["due_date"]) == 10


# ----------------------------------------------------------------- checks

def test_the_checks_sheet_counts_the_data_not_its_own_headers(wb):
    """Every check read exactly two on a clean workbook, because COUNTIF over
    the whole column counted the header and the note above it."""
    ws = wb["Checks"]
    r, seen = 5, 0
    while ws.cell(row=r, column=1).value:
        formula = str(ws.cell(row=r, column=3).value)
        assert "$4:" in formula, f"row {r} counts from the top of the column"
        assert formula.startswith("=COUNTIF("), formula
        seen += 1
        r += 1
    assert seen == sum(1 for key in CALC for h, *_ in CALC[key]
                       if h.startswith("check_"))


def test_every_check_is_named_and_explained(wb):
    ws = wb["Checks"]
    r = 5
    while ws.cell(row=r, column=1).value:
        assert ws.cell(row=r, column=2).value
        assert ws.cell(row=r, column=4).value in (
            "makes a figure wrong", "worth fixing", "note")
        assert len(str(ws.cell(row=r, column=5).value)) > 20
        r += 1


def test_a_formula_never_refers_to_a_grey_column_that_does_not_exist():
    """The ageing bucket referred to the wrong letter and reported a 93-day
    invoice as over 180. Referring to grey columns by name makes that a build
    failure rather than a wrong number."""
    for key, block in CALC.items():
        names = {h for h, *_ in block}
        for header, formula, _fmt, _note in block:
            for token in formula.split("%")[1::2]:
                assert token in names, f"{key}.{header} refers to %{token}%"


def test_every_grey_formula_is_guarded_on_the_row_having_data(wb):
    for key, title in TITLES.items():
        ws = wb[title]
        guard_col = [c.column for c in ws[3] if c.value == ROW_KEY[key]][0]
        letter = openpyxl.utils.get_column_letter(guard_col)
        for n in range(len(CALC.get(key, []))):
            f = str(ws.cell(row=ws.max_row, column=GREY_START + n).value)
            assert f.startswith(f'=IF({letter}{ws.max_row}=""'), f"{title}: {f[:60]}"


# ------------------------------------------- the arithmetic, against Excel

HAS_SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")


def recalculate(src: Path, workdir: Path) -> Path:
    """LibreOffice computes the formulas and writes the values back.

    openpyxl writes formulas with no cached value, so a freshly built
    workbook reads back as None everywhere. Converting it with LibreOffice
    recalculates on the way through, which is enough to check the arithmetic
    without depending on anything outside this repository.
    """
    out = workdir / "converted"
    out.mkdir(exist_ok=True)
    subprocess.run(
        [HAS_SOFFICE, "--headless", "--norestore",
         f"-env:UserInstallation=file://{workdir / 'profile'}",
         "--convert-to", "xlsx:Calc MS Excel 2007 XML",
         "--outdir", str(out), str(src)],
        check=True, capture_output=True, timeout=600,
    )
    return out / src.name


@pytest.mark.skipif(not HAS_SOFFICE, reason="needs LibreOffice")
@pytest.mark.skipif(os.environ.get("SOP_TEST_XLSX_RECALC") != "1",
                    reason="set SOP_TEST_XLSX_RECALC=1 (takes about a minute)")
def test_the_grey_arithmetic_agrees_with_the_engine(book, tmp_path):
    """Excel's own numbers, not Python's, against the engine's.

    Everything above proves the workbook imports. This proves the arithmetic
    written into it is the same arithmetic the product does, which is what
    makes it usable as a check before upload. It is also the test that would
    have caught the ageing bucket reading the wrong column.
    """
    src = tmp_path / "model.xlsx"
    shutil.copy(book, src)
    values = openpyxl.load_workbook(recalculate(src, tmp_path), data_only=True)

    ar = values["Receivables"]
    heads = {c.value: c.column for c in ar[3] if c.value}
    grey = {c.value: c.column for c in ar[3]
            if c.value and str(c.value).startswith("calc_")}

    invoices, _ = parse.parse("receivables", sheet_to_csv(book, "Receivables"))
    by_no = {i.invoice_no: i for i in invoices}

    from engine.modules.receivables import bucket_for

    checked = 0
    for row in range(4, 4 + len(invoices)):
        no = ar.cell(row=row, column=heads["invoice_no"]).value
        if not no:
            continue
        inv = by_no[no]
        open_amount = ar.cell(row=row, column=grey["calc_open_amount"]).value
        assert open_amount == pytest.approx(inv.amount - inv.amount_paid, abs=1)
        days = ar.cell(row=row, column=grey["calc_days_overdue"]).value
        assert days == max(0, (AS_OF - inv.due_date).days), no
        bucket = ar.cell(row=row, column=grey["calc_bucket"]).value
        assert bucket == bucket_for(days), f"{no}: {days} days read as {bucket}"
        checked += 1
    assert checked == len(invoices)

    # Stock value, and cover against the engine's own mean monthly demand.
    inv_ws = values["Inventory"]
    ih = {c.value: c.column for c in inv_ws[3] if c.value}
    ig = {c.value: c.column for c in inv_ws[3]
          if c.value and str(c.value).startswith("calc_")}
    for row in range(4, 4 + 12):
        oh = inv_ws.cell(row=row, column=ih["on_hand"]).value
        uc = inv_ws.cell(row=row, column=ih["unit_cost"]).value
        assert inv_ws.cell(row=row, column=ig["calc_stock_value"]).value \
            == pytest.approx(oh * uc, abs=1)
        po = inv_ws.cell(row=row, column=ih["open_po"]).value or 0
        assert inv_ws.cell(row=row, column=ig["calc_position"]).value \
            == pytest.approx(oh + po, abs=0.01)

    # And the blank form rows stay blank rather than each reporting a fault.
    last = inv_ws.max_row
    for col in ig.values():
        assert inv_ws.cell(row=last, column=col).value in (None, ""), \
            f"a blank row reported something in column {col}"


@pytest.mark.skipif(not HAS_SOFFICE, reason="needs LibreOffice")
@pytest.mark.skipif(os.environ.get("SOP_TEST_XLSX_RECALC") != "1",
                    reason="set SOP_TEST_XLSX_RECALC=1 (takes about a minute)")
def test_a_clean_workbook_reports_only_what_the_product_reports(book, tmp_path):
    """The Checks sheet counted its own headers, so a clean workbook showed
    two of every fault. What is left should be only the findings the product
    itself raises on the same data."""
    src = tmp_path / "model.xlsx"
    shutil.copy(book, src)
    values = openpyxl.load_workbook(recalculate(src, tmp_path), data_only=True)

    ws = values["Checks"]
    raised, r = {}, 5
    while ws.cell(row=r, column=1).value:
        n = ws.cell(row=r, column=3).value or 0
        if n:
            raised[f"{ws.cell(row=r, column=1).value}: "
                   f"{ws.cell(row=r, column=2).value}"] = n
        r += 1

    # The sample pack is deliberately imperfect in exactly two ways, and both
    # are findings the product raises too: one line that expires before it is
    # used, and one placement too new to judge.
    assert raised == {"Inventory: expiry before cover": 1,
                      "Machines: too new to judge": 1}, raised


# ------------------------------------------------- the whole model, uploaded

def _tweak(src, fn, dst):
    shutil.copy(src, dst)
    wb = openpyxl.load_workbook(dst)
    fn(wb)
    wb.save(dst)
    return dst


def test_the_whole_workbook_imports_as_one_file(book):
    from engine import workbook as reader
    read = reader.read(Path(book).read_bytes())
    assert set(read["texts"]) == set(samples.ORDER)
    assert not read["notes"]
    for key, text in read["texts"].items():
        assert parse.missing_columns(key, text) == [], key
        records, rejected = parse.parse(key, text)
        assert not rejected and records, key


def test_the_settings_sheet_travels_with_the_figures(book, tmp_path):
    """A workbook carrying an opening balance should produce a cash view with
    balances in it, without the number being typed in again on screen."""
    from engine import workbook as reader

    def set_balance(wb):
        for row in wb["Settings"].iter_rows(min_row=2, max_col=2):
            if row[0].value == "opening_balance":
                row[1].value = 18_000_000

    path = _tweak(book, set_balance, tmp_path / "balance.xlsx")
    carried = reader.settings(Path(path).read_bytes())
    assert carried["opening_balance"] == "18000000"
    assert carried["as_of"] == "2026-11-20"
    assert carried["cash_buffer"] == "2000000"


def test_a_missing_required_column_is_refused_by_name(book, tmp_path):
    from engine import workbook as reader

    def drop_due_date(wb):
        ws = wb["Receivables"]
        for c in ws[3]:
            if c.value == "due_date":
                ws.delete_cols(c.column)
                return

    path = _tweak(book, drop_due_date, tmp_path / "missing.xlsx")
    with pytest.raises(reader.WorkbookError) as exc:
        reader.read(Path(path).read_bytes())
    assert "due_date" in str(exc.value) and "Receivables" in str(exc.value)


def test_a_renamed_sheet_is_reported_not_skipped(book, tmp_path):
    """Skipping it silently produced an answer missing a whole domain, which
    is the failure the CSV path refuses too."""
    from engine import workbook as reader

    path = _tweak(book, lambda wb: setattr(wb["Inventory"], "title", "Stock list"),
                  tmp_path / "renamed.xlsx")
    read = reader.read(Path(path).read_bytes())
    assert "inventory" not in read["texts"]
    assert read["notes"], "nothing said about the sheet that was not found"
    note = " ".join(read["notes"])
    assert "Inventory" in note
    assert "Stock list" in note, "the renamed sheet should be named as a hint"


def test_an_empty_workbook_is_refused(book, tmp_path):
    from engine import workbook as reader

    def empty_them(wb):
        for t in TITLES.values():
            ws = wb[t]
            ws.delete_rows(4, ws.max_row)

    path = _tweak(book, empty_them, tmp_path / "empty.xlsx")
    with pytest.raises(reader.WorkbookError, match="empty"):
        reader.read(Path(path).read_bytes())


def test_something_that_is_not_a_workbook_says_so():
    from engine import workbook as reader
    with pytest.raises(reader.WorkbookError, match="could not be opened"):
        reader.read(b"item_key,on_hand\nABC,5\n")


def test_a_workbook_of_the_wrong_shape_points_at_the_model(tmp_path):
    from engine import workbook as reader
    other = openpyxl.Workbook()
    other.active.title = "Sheet1"
    other.active["A1"] = "something else"
    path = tmp_path / "other.xlsx"
    other.save(path)
    with pytest.raises(reader.WorkbookError) as exc:
        reader.read(path.read_bytes())
    assert "none of the sheets" in str(exc.value)


def test_a_reordered_sheet_still_reads(book, tmp_path):
    """Columns are found by name, so moving one must not change the import."""
    from engine import workbook as reader

    def move_a_column(wb):
        ws = wb["Machines"]
        ws.insert_cols(1)
        ws.cell(row=3, column=1, value="notes_of_my_own")
        for r in range(4, 12):
            ws.cell(row=r, column=1, value="mine")

    path = _tweak(book, move_a_column, tmp_path / "reordered.xlsx")
    read = reader.read(Path(path).read_bytes())
    records, rejected = parse.parse("machines", read["texts"]["machines"])
    assert records and not rejected


def test_the_whole_workbook_reads_back_as_the_datasets_it_holds(book):
    """The import path, tested at the reader rather than over HTTP.

    The upload endpoint went with the old demo shell; the capability did
    not, and it is how a business with a year of history gets that
    history in without typing it twice. When the new app grows an upload
    screen it will call exactly this.
    """
    from engine.workbook import read

    with open(book, "rb") as fh:
        out = read(fh.read())

    assert out["notes"] == [], out["notes"]
    texts = out["texts"]
    assert len(texts["inventory"].splitlines()) == 37     # 36 rows + header
    assert len(texts["demand"].splitlines()) == 433
    # Every dataset the parser knows has come back, so a sheet cannot be
    # silently dropped on the way in.
    from engine import samples
    assert set(texts) == set(samples.ORDER)
