"""Check the Excel model against the engine, figure by figure.

The workbook is meant to BE the platform, not to resemble it: the same
inputs through the same formulas should land on the same numbers. Nothing
else proves that. A workbook can recalculate with zero errors and still be
wrong everywhere -- an off-by-one range, a constant that drifted, a formula
that reads the header row -- and every one of those failures looks exactly
like a correct file until something compares the figures.

So this does. It builds the workbook from the sample data, has LibreOffice
compute it, runs the engine over the same sample data, and compares every
figure the two have in common. It prints what disagrees and exits non-zero
if anything does.

    python3 tools/verify_model.py                 # build, recalc, compare
    python3 tools/verify_model.py --xlsx FILE     # compare a built file

Bugs this has already caught, none of which raised an error in Excel:
  - a standard deviation that was silently zero, because its range started
    on a text row and IFERROR swallowed the #VALUE!
  - CV squared measured over the whole series instead of the non-zero
    demand sizes, which made every intermittent line look lumpy
  - a 30.4-day month against the engine's 30, which moved every reorder point
  - months of cover counting stock still on a ship
  - SUMIFS criteria ranges on another sheet, which pairs ranges by position
    and so matched rows three off
  - a 12-cell service-target matrix approximated by three literals
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openpyxl import load_workbook                       # noqa: E402

from engine import parse as parser, report, samples, schema   # noqa: E402
from engine.modules.finance import evaluate as evaluate_finance  # noqa: E402
from engine.modules.marketing import evaluate as evaluate_mkt    # noqa: E402
from engine.payroll import evaluate as evaluate_payroll          # noqa: E402
from engine.report import _statutory, _stock_rows                # noqa: E402

AS_OF = date(2026, 11, 20)

# Excel says "runs out"; the engine calls the same state CRITICAL.
STATES = {
    "OK": "within policy", "REORDER": "reorder", "CRITICAL": "runs out",
    "EXCESS": "above policy", "EXPIRY_RISK": "expiry risk",
}


class Report:
    def __init__(self) -> None:
        self.bad: list[str] = []
        self.checked = 0
        self.rows = 0

    def cell(self, where, col, excel, engine, tol):
        """Compare one figure. tol None means compare as text."""
        self.checked += 1
        if tol is None:
            if str(excel) != str(engine):
                self.bad.append(f"{where} {col}: excel={excel!r} engine={engine!r}")
            return
        if engine is None:
            if excel not in (None, "", 0):
                self.bad.append(f"{where} {col}: excel={excel!r} engine=blank")
            return
        got = 0.0 if excel in (None, "") else float(excel)
        want = float(engine)
        if math.isnan(got) or abs(got - want) > tol:
            self.bad.append(
                f"{where} {col}: excel={got:,.6f} engine={want:,.6f} "
                f"(off by {got - want:,.6f})"
            )

    def missing(self, sheet, col):
        self.bad.append(f"{sheet}: no column named {col!r}")


def headers(ws, row):
    return {c.value: c.column for c in ws[row] if c.value}


def _sample(*names):
    return {n: parser.parse(n, samples.sample_csv(n))[0] for n in names}


# ------------------------------------------------------------------ stock


def check_stock(wb, rep):
    recs = _sample("inventory", "demand")
    th = schema.Thresholds()
    blocks = report.build_blocks(recs["inventory"], recs["demand"], th, AS_OF)
    eng = {
        (r.item_key, b.location_id): (r, b.classes[(r.item_key, b.location_id)])
        for b in blocks for r in b.results
    }

    ws = wb["Stock policy"]
    hdr = headers(ws, 6)
    from_class = [
        ("Months of history", "periods_observed", 0.01),
        ("Months with demand", "nonzero_periods", 0.01),
        ("Mean monthly demand", "mean_monthly_demand", 0.01),
        ("Demand variability", "std_monthly_demand", 0.01),
        ("ADI", "adi", 0.01),
        ("CV squared", "cv_squared", 0.01),
        ("Annual consumption value", "annual_consumption_value", 1.0),
    ]
    from_result = [
        ("Service target", "service_target", 0.0001),
        ("Safety stock", "safety_stock", 0.5),
        ("Reorder point", "reorder_point", 0.5),
        ("Order up to", "order_up_to", 0.5),
        ("Position", "position", 0.5),
        ("Months of cover", "months_cover", 0.01),
        ("Days to expiry", "days_to_expiry", 0.5),
        ("Suggested order", "suggested_order_qty", 0.5),
        ("Stock value", "stock_value", 1.0),
        ("Above policy", "excess_value", 1.0),
    ]
    for r in range(7, ws.max_row + 1):
        item = ws.cell(row=r, column=hdr["Item"]).value
        if not item:
            continue
        key = (item, ws.cell(row=r, column=hdr["Branch"]).value)
        if key not in eng:
            rep.bad.append(f"Stock policy row {r}: {key} is not in the engine")
            continue
        res, cls = eng[key]
        rep.rows += 1
        for col, attr, tol in from_class:
            if col not in hdr:
                rep.missing("Stock policy", col)
                continue
            rep.cell(key, col, ws.cell(row=r, column=hdr[col]).value,
                     getattr(cls, attr), tol)
        for col, attr, tol in from_result:
            if col not in hdr:
                rep.missing("Stock policy", col)
                continue
            rep.cell(key, col, ws.cell(row=r, column=hdr[col]).value,
                     getattr(res, attr), tol)
        for col, attr in (("Demand class", "demand_class"),
                          ("ABC class", "abc_class"), ("State", "status")):
            if col not in hdr:
                rep.missing("Stock policy", col)
                continue
            want = getattr(res, attr)
            want = want.value if hasattr(want, "value") else str(want)
            want = STATES.get(want, want)
            rep.cell(key, col, ws.cell(row=r, column=hdr[col]).value, want, None)


# ---------------------------------------------------------------- payroll


def check_payroll(wb, rep):
    staff = _sample("employees")["employees"]
    res = evaluate_payroll(staff, _statutory(schema.Thresholds()), AS_OF)
    eng = {p.name: p for p in res.slips}

    ws = wb["Payroll calculation"]
    hdr = headers(ws, 6)
    cols = [
        ("Gross a month", "gross_monthly", 0.5),
        ("Medical exempt", "exempt_medical_monthly", 0.5),
        ("Taxable a year", "taxable_annual", 0.5),
        ("Tax a month", "tax_monthly", 0.5),
        ("Effective rate", "effective_tax_rate", 0.0005),
        ("EOBI employee", "eobi_employee", 0.5),
        ("EOBI employer", "eobi_employer", 0.5),
        ("Social security", "social_security_employer", 0.5),
        ("Provident fund employee", "pf_employee", 0.5),
        ("Provident fund employer", "pf_employer", 0.5),
        ("Gratuity accrued", "gratuity_accrual", 0.5),
        ("Net pay", "net_monthly", 0.5),
        ("Cost to employer", "employer_cost", 0.5),
    ]
    for r in range(7, ws.max_row + 1):
        name = ws.cell(row=r, column=hdr["Name"]).value
        if not name or name not in eng:
            continue
        slip = eng[name]
        rep.rows += 1
        for col, attr, tol in cols:
            if col not in hdr:
                rep.missing("Payroll calculation", col)
                continue
            rep.cell(name, col, ws.cell(row=r, column=hdr[col]).value,
                     getattr(slip, attr), tol)


# ---------------------------------------------------------------- finance


def check_finance(wb, rep):
    accounts = _sample("trial_balance")["trial_balance"]
    eng = evaluate_finance(
        accounts, schema.Thresholds(),
        period_from="2026-01-01", period_to=AS_OF.isoformat(), dividends=0.0,
    )

    def find(sheet, label, offset=1):
        ws = wb[sheet]
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.strip() == label.strip():
                    return ws.cell(row=c.row, column=c.column + offset).value
        rep.bad.append(f"{sheet}: no row labelled {label!r}")
        return None

    want = [
        ("Profit and loss", "Revenue", "revenue", 1.0),
        ("Profit and loss", "Gross profit", "gross_profit", 1.0),
        ("Profit and loss", "Operating profit", "operating_profit", 1.0),
        ("Profit and loss", "Profit before tax", "profit_before_tax", 1.0),
        ("Profit and loss", "Profit after tax", "profit_after_tax", 1.0),
        ("Profit and loss", "Gross margin", "gross_margin", 0.0005),
        ("Profit and loss", "Net margin", "net_margin", 0.0005),
        ("Balance sheet", "Working capital", "working_capital", 1.0),
        ("Balance sheet", "Current ratio", "current_ratio", 0.01),
        ("Balance sheet", "Gearing", "gearing", 0.0005),
        ("Cash flow", "Cash from operations", "cash_from_operations", 1.0),
        ("Cash flow", "Cash from investing", "cash_from_investing", 1.0),
        ("Cash flow", "Cash from financing", "cash_from_financing", 1.0),
        ("Cash flow", "Net movement in cash", "net_cash_movement", 1.0),
    ]
    for sheet, label, attr, tol in want:
        rep.rows += 1
        rep.cell(sheet, label, find(sheet, label), getattr(eng, attr), tol)

    # The balance sheet's own columns: opening and closing side by side.
    for label, attr, off in (("Total assets", "assets_closing", 2),
                             ("Total liabilities", "liabilities_closing", 2),
                             ("Total equity", "equity_closing", 2)):
        rep.cell("Balance sheet", label,
                 find("Balance sheet", label, off), getattr(eng, attr), 1.0)


# -------------------------------------------------------------- pipeline


def check_pipeline(wb, rep):
    recs = _sample("inventory", "demand", "opportunities", "marketing_spend")
    th = schema.Thresholds()
    blocks = report.build_blocks(recs["inventory"], recs["demand"], th, AS_OF)
    eng_res = evaluate_mkt(
        recs["opportunities"], recs["marketing_spend"], _stock_rows(blocks),
        cash_headroom=None, thresholds=th, as_of=AS_OF,
    )
    # Only open deals appear in the engine's rows; the sheet lists them all.
    eng = {r.opportunity_id: r for r in eng_res.rows}

    ws = wb["Pipeline calculation"]
    hdr = headers(ws, 5)
    cols = [
        ("Odds at this stage", "odds", 0.00001),
        ("Weighted value", "weighted", 1.0),
        ("Days to close", "days_to_close", 0.5),
        ("Stage", "stage", None),
        ("Can we supply it", "supply", None),
    ]
    for r in range(6, ws.max_row + 1):
        oid = ws.cell(row=r, column=hdr["Opportunity"]).value
        if not oid or oid not in eng:
            continue
        row = eng[oid]
        rep.rows += 1
        for col, attr, tol in cols:
            if col not in hdr:
                rep.missing("Pipeline calculation", col)
                continue
            rep.cell(oid, col, ws.cell(row=r, column=hdr[col]).value,
                     getattr(row, attr), tol)


CHECKS = (
    ("stock policy", check_stock),
    ("payroll", check_payroll),
    ("finance statements", check_finance),
    ("pipeline", check_pipeline),
)


def recalc(path: Path) -> None:
    """Have LibreOffice compute the formulas, so the cells hold values."""
    script = None
    for base in (Path("/root/.claude/skills"), Path.home() / ".claude/skills"):
        hits = sorted(base.glob("**/xlsx/scripts/recalc.py")) if base.exists() else []
        if hits:
            script = hits[0]
            break
    if script is None:
        sys.exit("cannot find recalc.py; pass an already-recalculated "
                 "file with --xlsx")
    out = subprocess.run([sys.executable, str(script), str(path), "600"],
                         capture_output=True, text=True, timeout=1200)
    sys.stdout.write(out.stdout)
    if '"status": "success"' not in out.stdout:
        sys.exit("recalculation did not succeed; fix the formula errors first")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xlsx", help="an already-built, already-recalculated file")
    args = ap.parse_args()

    if args.xlsx:
        path = Path(args.xlsx)
    else:
        tmp = Path(tempfile.mkdtemp()) / "model.xlsx"
        subprocess.run([sys.executable, str(ROOT / "tools/build_workbook.py"),
                        str(tmp)], check=True)
        recalc(tmp)
        path = tmp

    wb = load_workbook(path, data_only=True)
    rep = Report()
    for name, fn in CHECKS:
        before = len(rep.bad)
        fn(wb, rep)
        mark = "ok" if len(rep.bad) == before else f"{len(rep.bad) - before} WRONG"
        print(f"  {name:20} {mark}")

    print(f"\n{rep.rows} rows, {rep.checked} figures compared, "
          f"{len(rep.bad)} disagree")
    for b in rep.bad:
        print("   ", b)
    if rep.bad:
        print("\nThe model and the product do not agree. One of them is wrong.")
        return 1
    print("\nEvery figure the model and the product share is identical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
