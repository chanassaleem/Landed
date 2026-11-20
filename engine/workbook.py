"""Reading the data model workbook back in.

The workbook is the field specification and the place a distributor keeps
their figures, so it has to come back the way it went out: one file, every
dataset, no renaming and no saving six CSVs by hand.

Only the white columns are read. The grey ones are formulas -- the arithmetic
written out so it can be checked by eye before uploading -- and they are
ignored here, which is what lets the same sheet be both a working sheet and
an import.

What this will not do is guess. A sheet that has been renamed, or a column
that has been deleted, is refused with the name of the sheet and the column,
because the alternative is a dashboard computed from the wrong column.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime

from engine.samples import DATASETS, ORDER

# Sheet titles as the workbook writes them.
SHEETS = {
    "inventory": "Inventory",
    "demand": "Demand",
    "receivables": "Receivables",
    "service": "Service",
    "machines": "Machines",
    "commitments": "Commitments",
    "trial_balance": "Trial balance",
    "employees": "Payroll",
    "opportunities": "Pipeline",
    "marketing_spend": "Marketing spend",
}

HEADER_ROW = 3          # row 1 is the hint, row 2 the field notes
FIRST_DATA_ROW = 4
WORKED_OUT = ("calc_", "check_")


class WorkbookError(ValueError):
    """Something about the file means it cannot be read without guessing."""


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        return value.strip()
    return str(value)


def sheet_to_csv(ws, dataset: str) -> tuple[str, int]:
    """One sheet as the CSV the importer already reads.

    Returns the text and how many rows carried anything, so an empty sheet
    can be reported as empty rather than as a file that failed.
    """
    spec = [c.name for c in DATASETS[dataset].columns]
    required = {c.name for c in DATASETS[dataset].columns if c.required}

    header_cells = {
        _cell_text(c.value): c.column
        for c in ws[HEADER_ROW] if _cell_text(c.value)
    }
    present = {h: col for h, col in header_cells.items()
               if not h.startswith(WORKED_OUT)}

    missing = sorted(required - set(present))
    if missing:
        raise WorkbookError(
            f"The {ws.title} sheet is missing {', '.join(missing)}. "
            f"Its header row is row {HEADER_ROW}; if you have added columns, "
            f"add them after the worked-out ones rather than in place of a "
            f"heading."
        )

    # Written in the template's order, not the sheet's, so a reordered sheet
    # still produces a file the parser reads the same way.
    heads = [h for h in spec if h in present]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(heads)

    rows = 0
    for row in ws.iter_rows(min_row=FIRST_DATA_ROW, max_row=ws.max_row):
        values, filled = [], False
        for head in heads:
            value = row[present[head] - 1].value
            text = _cell_text(value)
            if text:
                filled = True
            values.append(text)
        if filled:
            writer.writerow(values)
            rows += 1
    return out.getvalue(), rows


def read(data: bytes) -> dict:
    """Every data sheet of the workbook, as CSV text keyed by dataset.

    Returns {"texts": {dataset: csv}, "notes": [...]}: the notes say what was
    not there, because a missing sheet has to be said out loud rather than
    quietly left out of the answer.

    A sheet that is present but empty is left out rather than sent on as a
    header with no rows: an empty file and an absent one mean different
    things to the engine, and "you did not supply this" is the truthful one.
    """
    try:
        from openpyxl import load_workbook
    except ImportError as exc:                       # pragma: no cover
        raise WorkbookError(
            "This server cannot read Excel files. Upload the CSVs instead."
        ) from exc

    try:
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        raise WorkbookError(
            f"That file could not be opened as a workbook ({exc}). If it was "
            f"exported from another system, save it as .xlsx first."
        ) from exc

    have = set(wb.sheetnames)
    wanted = {SHEETS[k] for k in ORDER}
    if not (have & wanted):
        raise WorkbookError(
            f"This workbook has none of the sheets the model uses "
            f"({', '.join(sorted(wanted))}). It holds: "
            f"{', '.join(sorted(have)) or 'nothing'}. Download the data model "
            f"from this screen and fill that in."
        )

    texts: dict[str, str] = {}
    empty: list[str] = []
    absent: list[str] = []
    for key in ORDER:
        title = SHEETS[key]
        if title not in have:
            absent.append(title)
            continue
        text, rows = sheet_to_csv(wb[title], key)
        if rows:
            texts[key] = text
        else:
            empty.append(title)

    if not texts:
        raise WorkbookError(
            "Every sheet in that workbook is empty. Fill in at least one "
            "before uploading it."
        )

    # A sheet that is not there is reported rather than passed over. Silently
    # skipping one produced a dashboard that looked complete and was missing a
    # domain, which is the failure this product refuses for CSVs too. The
    # commonest cause is a renamed sheet, so the extra ones are named: that is
    # usually enough to see what happened.
    notes = []
    if absent:
        spare = sorted(have - wanted - {"Read me", "Settings", "Companies",
                                        "Branches", "Checks", "Lists",
                                        "Accounts"})
        notes.append(
            f"{len(absent)} of the model's sheets were not in this workbook: "
            f"{', '.join(absent)}. Those domains are absent from the answer "
            f"rather than estimated."
            + (f" The workbook does hold {', '.join(spare)}; if one of those "
               f"is a renamed sheet, rename it back." if spare else "")
        )
    if empty:
        notes.append(
            f"{', '.join(empty)} "
            f"{'was' if len(empty) == 1 else 'were'} present but empty."
        )
    return {"texts": texts, "notes": notes}


def settings(data: bytes) -> dict[str, str]:
    """The Settings sheet, as the form fields the analyse endpoint reads.

    Carried over so the assumptions travel with the figures: a workbook with
    an opening balance in it should produce a cash view with balances in it,
    without the person having to retype the number here as well.
    """
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception:
        return {}
    if "Settings" not in wb.sheetnames:
        return {}

    # Only the ones the endpoint actually takes. Anything else on that sheet
    # is for the reader, not for the engine.
    FIELDS = {
        "as_of_date": "as_of",
        "target_cover_months": "target_cover",
        "excess_cover_months": "excess_months",
        "cost_of_capital_annual": "cost_of_capital",
        "target_dso_days": "target_dso",
        "labour_rate_per_hour": "labour_rate",
        "payback_target_months": "payback_target",
        "min_transfer_value": "min_transfer_value",
        "monthly_revenue": "monthly_revenue",
        "opening_balance": "opening_balance",
        "cash_buffer": "cash_buffer",
        "collection_rate": "collection_rate",
        "forecast_horizon_months": "forecast_horizon",
    }
    ws = wb["Settings"]
    out: dict[str, str] = {}
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=2):
        name = _cell_text(row[0].value)
        field = FIELDS.get(name)
        if not field:
            continue
        value = _cell_text(row[1].value)
        if value:
            out[field] = value
    return out
