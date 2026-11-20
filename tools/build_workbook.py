"""Build the S&OP Insight data workbook.

One file covering every dataset the engine reads. It is three things at once:

  the field specification, because the white columns are exactly the headers
  the importer expects, in the order the templates use;

  the working database, until the real one is wired up, because a distributor
  can keep their figures here and upload the sheets;

  and a sanity check before upload, because the grey columns recompute in
  Excel what the engine would compute, and the Checks sheet counts the faults
  the engine would refuse the file for.

Colour carries one meaning, and only one:

  white   you type here. These columns are the import.
  grey    worked out for you. Formulas. Never type in these, and they are
          ignored on import.

The grey columns sit after the white ones on every sheet, so the whole sheet
can be saved as CSV and imported unchanged -- the parser matches columns by
name and ignores the ones it does not know.

Run:  python3 tools/build_workbook.py [out.xlsx]
"""

from __future__ import annotations

import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import parse, samples  # noqa: E402

FONT = "Arial"

INK = "FF1A1A1A"
MUTED = "FF6B6B6B"
WHITE = "FFFFFFFF"
GREY = "FFEFEFEF"          # worked out for you
GREY_HEAD = "FFD9D9D9"
WHITE_HEAD = "FFF7F7F7"
LINE = "FFBFBFBF"
ALARM = "FFC0392B"
OK = "FF1E7B34"

thin = Side(style="thin", color=LINE)
BOX = Border(left=thin, right=thin, top=thin, bottom=thin)

H1 = Font(name=FONT, size=14, bold=True, color=INK)
H2 = Font(name=FONT, size=11, bold=True, color=INK)
BODY = Font(name=FONT, size=10, color=INK)
NOTE = Font(name=FONT, size=9, color=MUTED, italic=True)
HEADW = Font(name=FONT, size=9, bold=True, color=INK)
HEADG = Font(name=FONT, size=9, bold=True, color=MUTED)

MONEY = '#,##0;(#,##0);-'
QTY = '#,##0.##'
PCT = '0.0%'
DATE = 'yyyy-mm-dd'
NUM1 = '0.0'

# How many blank rows to leave below the sample data, so the sheet is usable
# as a form rather than only as an example.
SPARE = 120


# ---------------------------------------------------------------- the grey

# Each entry: (header, formula with {r} for the row number, number format,
# and a note explaining what it is for.)
#
# Written as formulas rather than values so the sheet stays alive: change a
# unit cost and the cover, the value and the checks all move with it.

CALC = {
    "inventory": [
        ("calc_stock_value", "=IFERROR({oh}{r}*{uc}{r},0)", MONEY,
         "On hand times unit cost. What this line ties up."),
        ("calc_position", "=IFERROR({oh}{r}+{po}{r},0)", QTY,
         "On hand plus on order. What the reorder point is compared against."),
        ("calc_months_history",
         '=COUNTIFS(Demand!$C:$C,{ik}{r},Demand!$A:$A,{en}{r},Demand!$B:$B,{lo}{r})',
         "0",
         "Months of demand history found for this line. Under 6 and the "
         "forecast is a guess; under 3 and there is no forecast at all."),
        ("calc_mean_monthly",
         '=IFERROR(AVERAGEIFS(Demand!$E:$E,Demand!$C:$C,{ik}{r},'
         'Demand!$A:$A,{en}{r},Demand!$B:$B,{lo}{r}),0)', NUM1,
         "Mean monthly issues from the Demand sheet."),
        ("calc_months_cover",
         '=IF(AND(ISNUMBER(%calc_mean_monthly%{r}),%calc_mean_monthly%{r}>0),'
         'IFERROR(%calc_position%{r}/%calc_mean_monthly%{r},""),"")', NUM1,
         "Position divided by mean monthly demand. Blank when there is no "
         "demand to divide by, which is itself the finding."),
        ("check_branch_known",
         '=IF(COUNTIFS(Branches!$A:$A,{lo}{r})=0,"branch not set up","")',
         "@", "The branch code is not on the Branches sheet."),
        ("check_duplicate_line",
         '=IF(AND({ik}{r}<>"",COUNTIFS($A:$A,{en}{r},$B:$B,{lo}{r},'
         '$C:$C,{ik}{r})>1),"same item twice","")', "@",
         "The same item appears twice for one branch. The engine would take "
         "one of them and you would not know which."),
        ("check_cost_blank",
         '=IF(OR({uc}{r}="",{uc}{r}<=0),"no unit cost","")', "@",
         "Without a cost this line has no value and cannot be ranked."),
        ("check_lead_time",
         '=IF(OR({lt}{r}="",{lt}{r}<=0),"no lead time","")', "@",
         "Safety stock cannot be computed without one."),
        ("check_expiry_before_cover",
         '=IF(AND({ex}{r}<>"",ISNUMBER(%calc_months_cover%{r}),'
         '({ex}{r}-Settings!$B$2)/30.4<%calc_months_cover%{r}),'
         '"expires before it is used","")',
         "@",
         "The stock dies before the demand arrives. A write-off, not a delay."),
    ],
    "demand": [
        ("check_item_known",
         '=IF(COUNTIFS(Inventory!$C:$C,{ik}{r})=0,"item not in Inventory","")',
         "@", "Demand for an item the inventory file does not contain."),
        ("check_period_format",
         '=IF(OR(LEN({pe}{r})<>7,MID({pe}{r},5,1)<>"-"),"period not YYYY-MM","")',
         "@", "The period must read 2026-04, or the month is dropped."),
        ("check_duplicate_period",
         '=IF(AND({pe}{r}<>"",COUNTIFS($A:$A,{en}{r},$B:$B,{lo}{r},'
         '$C:$C,{ik}{r},$D:$D,{pe}{r})>1),"month twice","")', "@",
         "One month twice for one item doubles that month's demand."),
        ("check_negative",
         '=IF(AND(ISNUMBER({qt}{r}),{qt}{r}<0),"negative quantity","")', "@",
         "A return belongs in its own column, not as negative demand."),
    ],
    "receivables": [
        ("calc_open_amount",
         '=IFERROR({am}{r}-IF({ap}{r}="",0,{ap}{r}),0)', MONEY,
         "Invoice less what has been received. What is actually owed."),
        ("calc_days_overdue",
         '=IF({du}{r}="","",MAX(0,Settings!$B$2-{du}{r}))', "0",
         "Days past the due date, as at the as-of date on Settings."),
        ("calc_bucket",
         '=IF(%calc_days_overdue%{r}="","",'
         'IF(%calc_days_overdue%{r}<=0,"current",'
         'IF(%calc_days_overdue%{r}<=30,"1-30",'
         'IF(%calc_days_overdue%{r}<=60,"31-60",'
         'IF(%calc_days_overdue%{r}<=90,"61-90",'
         'IF(%calc_days_overdue%{r}<=180,"91-180","180+"))))))', "@",
         "The ageing bucket this invoice falls in."),
        ("check_due_before_invoice",
         '=IF(AND({iv}{r}<>"",{du}{r}<>"",{du}{r}<{iv}{r}),'
         '"due before it was raised","")', "@",
         "Terms cannot be negative. Usually a swapped column."),
        ("check_duplicate_invoice",
         '=IF(AND({no}{r}<>"",COUNTIFS($A:$A,{en}{r},$C:$C,{no}{r})>1),'
         '"invoice twice","")', "@",
         "The same invoice number twice in one company doubles the balance."),
        ("check_paid_over",
         '=IF(AND(ISNUMBER({ap}{r}),ISNUMBER({am}{r}),{ap}{r}>{am}{r}),'
         '"paid more than billed","")', "@",
         "Either a credit note is missing or the columns are swapped."),
        ("check_customer_name",
         '=IF(AND({ck}{r}<>"",{cn}{r}<>"",COUNTIFS($D:$D,{ck}{r},$E:$E,"<>"&{cn}{r})>0),'
         '"same code, other name","")', "@",
         "One customer code with two spellings. The group total is then split "
         "across what looks like two customers."),
    ],
    "service": [
        ("calc_hours_unbilled",
         '=IFERROR({lh}{r}-IF({lb}{r}="",0,{lb}{r}),0)', NUM1,
         "Hours worked and never invoiced."),
        ("calc_parts_unbilled",
         '=IFERROR(IF({pc}{r}="",0,{pc}{r})-IF({pch}{r}="",0,{pch}{r}),0)', MONEY,
         "Parts fitted and never charged."),
        ("calc_is_open", '=IF({cd}{r}="","open","closed")', "@",
         "A job with no closed date is still open."),
        ("check_closed_before_opened",
         '=IF(AND({cd}{r}<>"",{od}{r}<>"",{cd}{r}<{od}{r}),'
         '"closed before it opened","")', "@", "A swapped pair of dates."),
        ("check_machine_known",
         '=IF(COUNTIFS(Machines!$C:$C,{mk}{r})=0,"machine not in Machines","")',
         "@",
         "Wastage cannot be tied to a placement, so the machine's true cost "
         "of ownership is understated."),
        ("check_billed_over_worked",
         '=IF(AND(ISNUMBER({lb}{r}),ISNUMBER({lh}{r}),{lb}{r}>{lh}{r}),'
         '"billed more than worked","")', "@",
         "Over-billing, or hours entered in the wrong column."),
        ("check_duplicate_job",
         '=IF(AND({jn}{r}<>"",COUNTIFS($A:$A,{en}{r},$C:$C,{jn}{r})>1),'
         '"job twice","")', "@",
         "The same job number twice doubles the wastage."),
    ],
    "machines": [
        ("calc_months_elapsed",
         '=IF({pd}{r}="","",ROUND((Settings!$B$2-{pd}{r})/30.4,1))', NUM1,
         "Months since it went in, as at the as-of date."),
        ("calc_margin_earned",
         '=IFERROR({rr}{r}*{rm}{r},0)', MONEY,
         "Reagent revenue times margin. What the placement has actually "
         "brought back."),
        ("calc_recovery_pct",
         '=IF(AND(ISNUMBER({cc}{r}),{cc}{r}>0),'
         'IFERROR(%calc_margin_earned%{r}/{cc}{r},""),"")',
         PCT, "Margin earned against capital cost."),
        ("calc_utilisation",
         '=IF(AND(ISNUMBER({em}{r}),{em}{r}>0),IFERROR({am}{r}/{em}{r},""),"")',
         PCT, "Actual tests against the volume it was placed against."),
        ("check_margin_as_percent",
         '=IF(AND(ISNUMBER({rm}{r}),{rm}{r}>1),"margin looks like a percent","")',
         "@",
         "0.36 means 36 per cent. A 36 here multiplies every recovery figure "
         "by a hundred."),
        ("check_too_new_to_judge",
         '=IF(AND(ISNUMBER(%calc_months_elapsed%{r}),'
         '%calc_months_elapsed%{r}<6),"too new to judge","")', "@",
         "Under six months a payback figure says more about the ramp-up than "
         "about the placement."),
        ("check_duplicate_machine",
         '=IF(AND({mk}{r}<>"",COUNTIFS($C:$C,{mk}{r})>1),'
         '"machine twice","")', "@",
         "The same asset twice counts its capital twice."),
        ("check_contract_months",
         '=IF(OR({cm}{r}="",{cm}{r}<=0),"no contract length","")', "@",
         "Payback has nothing to land inside."),
    ],
    "commitments": [
        ("calc_days_until_due",
         '=IF({dd}{r}="","",{dd}{r}-Settings!$B$2)', "0",
         "Negative means it was already due."),
        ("calc_in_horizon",
         '=IF({dd}{r}="","",IF(AND(%calc_days_until_due%{r}>=-7,'
         '%calc_days_until_due%{r}<=Settings!$B$14*7),'
         '"in the horizon","outside"))', "@",
         "Whether it lands inside the cash horizon on Settings."),
        ("check_recurs_without_period",
         '=IF(AND(LOWER({ir}{r})="yes",OR({re}{r}="",{re}{r}<=0)),'
         '"recurring with no period","")', "@",
         "Marked recurring with no interval, so it is counted once and the "
         "rest of the horizon is understated."),
        ("check_amount",
         '=IF(OR({am}{r}="",{am}{r}<=0),"no amount","")', "@",
         "A commitment with no amount moves no money."),
        ("check_kind",
         '=IF({ki}{r}="","",IF(COUNTIF(Lists!$D:$D,{ki}{r})=0,'
         '"unknown kind","")) ', "@",
         "Not one of the kinds the cash view understands. It still counts as "
         "an outflow, but it cannot be grouped."),
    ],
    "trial_balance": [
        ("calc_movement", "={cl}{r}-{op}{r}", MONEY,
         "Closing less opening. This is what the cash flow statement is "
         "built from."),
        ("check_line_known",
         '=IF(COUNTIF(Lists!$J:$J,{sl}{r})=0,"not a statement line","")', "@",
         "The statement_line is not one the statements use, so this account "
         "would drop out of the profit and the balance sheet."),
        ("check_duplicate_account",
         '=IF(AND({ac}{r}<>"",COUNTIFS($A:$A,{en}{r},$B:$B,{ac}{r})>1),'
         '"account twice","")', "@",
         "The same account code twice in one company doubles its figure."),
        ("check_sign",
         '=IF(AND(ISNUMBER({cl}{r}),{cl}{r}<0),"negative balance","")', "@",
         "Amounts are positive in their natural sense: revenue positive, "
         "liabilities positive. A negative here usually means a credit "
         "balance entered as a minus, which the statements will double."),
    ],
    "employees": [
        ("calc_gross_monthly",
         "={bm}{r}+{am}{r}+{mm}{r}", MONEY,
         "Basic plus allowances plus medical, before anything is deducted."),
        ("calc_medical_exempt",
         "=MIN({mm}{r},{bm}{r}*0.1)", MONEY,
         "Medical allowance is exempt up to a tenth of basic; the rest is "
         "taxable."),
        ("calc_taxable_annual",
         "=({bm}{r}+{am}{r}+{mm}{r}-%calc_medical_exempt%{r})*12", MONEY,
         "What the slabs are applied to."),
        ("check_pf_rate",
         '=IF(OR({ep}{r}>1,{qp}{r}>1),"provident fund looks like a percent","")',
         "@",
         "0.08 means 8%. An 8 here multiplies the contribution a hundredfold."),
        ("check_left_before_joined",
         '=IF(AND({lf}{r}<>"",{jd}{r}<>"",{lf}{r}<{jd}{r}),'
         '"left before joined","")', "@", "A swapped pair of dates."),
        ("check_duplicate_employee",
         '=IF(AND({ei}{r}<>"",COUNTIF($A:$A,{ei}{r})>1),"employee twice","")',
         "@", "The same person twice doubles the payroll cost."),
    ],
    "opportunities": [
        ("calc_days_to_close",
         '=IF({ec}{r}="","",{ec}{r}-Settings!$B$2)', "0",
         "Negative means the expected close date has already passed."),
        ("calc_age_days",
         '=IF({op}{r}="","",Settings!$B$2-{op}{r})', "0",
         "How long it has been open. A lead open for half a year is "
         "usually not a lead."),
        ("check_stage",
         '=IF(COUNTIF(Lists!$M:$M,{st}{r})=0,"unknown stage","")', "@",
         "Not one of the stages, so it is neither counted as open nor as "
         "closed."),
        ("check_closed_without_date",
         '=IF(AND(OR({st}{r}="won",{st}{r}="lost"),{cd}{r}=""),'
         '"closed with no date","")', "@",
         "A won or lost deal with no close date cannot be used to measure "
         "how long deals take."),
        ("check_item_known",
         '=IF({ik}{r}="","",IF(COUNTIF(Inventory!$C:$C,{ik}{r})=0,'
         '"item not in Inventory",""))', "@",
         "Without a matching item the supply check cannot run, so a tender "
         "you cannot serve will not be flagged."),
    ],
    "marketing_spend": [
        ("calc_days_ago", "=Settings!$B$2-{so}{r}", "0",
         "How long ago it was spent."),
        ("check_amount",
         '=IF(OR({am}{r}="",{am}{r}<=0),"no amount","")', "@",
         "Spend with no amount cannot be priced against anything."),
    ],
}

# Where each dataset's grey block starts. Fixed, and well clear of the widest
# white block, so the formulas above can refer to grey cells by letter without
# every one of them shifting when a column is added to a template.
GREY_START = 40   # column AN

# Data starts on row 4 on every input sheet: row 1 is the hint, row 2 the
# field notes, row 3 the headers. The reader in engine/workbook.py uses the
# same number, and they must not drift.
from engine.workbook import FIRST_DATA_ROW   # noqa: E402
from engine.schema import Thresholds         # noqa: E402
from engine.modules.inventory import DAYS_PER_MONTH  # noqa: E402


def col_refs(headers):
    """Letter for each header, so formulas name columns rather than guess."""
    return {h: get_column_letter(i + 1) for i, h in enumerate(headers)}


# Short names used in the formulas above, per dataset.
SHORT = {
    "inventory": {"en": "entity_id", "lo": "location_id", "ik": "item_key",
                  "oh": "on_hand", "po": "open_po", "uc": "unit_cost",
                  "lt": "lead_time_days", "ex": "expiry_date"},
    "demand": {"en": "entity_id", "lo": "location_id", "ik": "item_key",
               "pe": "period", "qt": "quantity"},
    "receivables": {"en": "entity_id", "no": "invoice_no", "ck": "customer_key",
                    "cn": "customer_name", "iv": "invoice_date",
                    "du": "due_date", "am": "amount", "ap": "amount_paid"},
    "service": {"en": "entity_id", "jn": "job_no", "mk": "machine_key",
                "od": "opened_date", "cd": "closed_date",
                "lh": "labour_hours", "lb": "labour_hours_billed",
                "pc": "parts_cost", "pch": "parts_charged"},
    "machines": {"en": "entity_id", "mk": "machine_key", "pd": "placed_date",
                 "cc": "capital_cost", "cm": "contract_months",
                 "rr": "reagent_revenue_to_date", "rm": "reagent_margin_pct",
                 "em": "expected_monthly_tests", "am": "actual_monthly_tests"},
    "commitments": {"en": "entity_id", "dd": "due_date", "am": "amount",
                    "ki": "kind", "ir": "is_recurring",
                    "re": "recurs_every_days"},
    "trial_balance": {"en": "entity_id", "ac": "account_code",
                      "sl": "statement_line", "op": "opening_balance",
                      "cl": "closing_balance"},
    "employees": {"ei": "employee_id", "en": "entity_id",
                  "bm": "basic_monthly", "am": "allowances_monthly",
                  "mm": "medical_monthly", "ep": "employer_pf_rate",
                  "qp": "employee_pf_rate", "jd": "joined", "lf": "left"},
    "opportunities": {"en": "entity_id", "oi": "opportunity_id",
                      "st": "stage", "ec": "expected_close", "op": "opened",
                      "cd": "closed", "ik": "item_key"},
    "marketing_spend": {"en": "entity_id", "so": "spent_on", "am": "amount"},
}

# Every grey formula is guarded on this column having something in it. The
# sheet carries blank rows so it can be used as a form, and without the guard
# each one reported a missing cost, an unknown branch and a duplicate of every
# other blank row: 122 phantom faults on a clean workbook, which is worse than
# no checks at all because it teaches you to ignore them.
ROW_KEY = {
    "inventory": "item_key", "demand": "item_key",
    "receivables": "invoice_no", "service": "job_no",
    "machines": "machine_key", "commitments": "label",
    "trial_balance": "account_code", "employees": "employee_id",
    "opportunities": "opportunity_id", "marketing_spend": "label",
}

DATE_COLUMNS = {"joined", "left", "expected_close", "opened", "closed",
                "spent_on", "expiry_date", "invoice_date", "due_date", "opened_date",
                "closed_date", "placed_date"}
MONEY_COLUMNS = {"opening_balance", "closing_balance", "basic_monthly",
                 "allowances_monthly", "medical_monthly", "value",
                 "unit_cost", "amount", "amount_paid", "parts_cost",
                 "parts_charged", "capital_cost", "reagent_revenue_to_date",
                 "service_cost_to_date", "revenue_per_test"}
PCT_COLUMNS = {"reagent_margin_pct", "employer_pf_rate", "employee_pf_rate"}


# ------------------------------------------------------------------ sheets


def sheet_title(key):
    from engine.workbook import SHEETS
    return SHEETS[key]


def write_readme(ws):
    ws.sheet_view.showGridLines = False
    rows = [
        ("S&OP Insight — the model", H1),
        ("", None),
        ("This is the whole product as a spreadsheet. The same inputs, the "
         "same formulas, the same numbers. Change an assumption on Settings "
         "and every figure downstream moves, as it would in the product.",
         BODY),
        ("", None),
        ("Two colours, one meaning each.", H2),
        ("White cells are yours to fill in. Those columns are the import: "
         "their headers are exactly what the engine reads.", BODY),
        ("Grey cells are worked out for you. They are formulas. Do not type "
         "in them, and do not delete them — they are ignored on import.", BODY),
        ("", None),
        ("How to use it", H2),
        ("1. Fill in Companies and Branches first. Every other sheet checks "
         "its codes against those two, so doing this first turns most "
         "mistakes into a message rather than a wrong number.", BODY),
        ("2. Set the as-of date on Settings. Every ageing and overdue figure "
         "in this workbook is measured from it.", BODY),
        ("3. Fill in the ten white data sheets. The sample rows are real in "
         "shape and fictional in content: overwrite them or delete them.",
         BODY),
        ("4. Read the Checks sheet. It counts the same faults the engine "
         "would refuse the file for. Nothing there should be left red.", BODY),
        ("5. Read Summary. It is the whole business on one screen, and it is "
         "where a wrong input usually shows up as an implausible answer.",
         BODY),
        ("6. Upload the whole workbook on the Data pack screen. It reads "
         "every sheet in one go — there is no need to export CSVs, though "
         "the product still accepts those.", BODY),
        ("", None),
        ("The assumptions are all in one place", H2),
        ("Everything the product treats as a judgement call is on Settings "
         "and nowhere else: the cover targets, the ABC cut-offs, the demand "
         "cut-offs, the service-target matrix, the minimum wage EOBI is "
         "charged on, the odds the pipeline falls back on. None of them is "
         "buried in a formula. Change one and the model recalculates the "
         "way the product would.", BODY),
        ("", None),
        ("Where the numbers come from", H2),
        ("The grey half is not a summary of the product — it is the product's "
         "arithmetic written out. Stock policy carries the demand "
         "classification, the ABC ranking and the safety stock. Profit and "
         "loss, Balance sheet and Cash flow are derived from the Trial "
         "balance, and the cash flow is built from the other two rather than "
         "entered. Payroll calculation works the salary tax slabs out band "
         "by band. Pipeline calculation weights each deal and checks it "
         "against the stock that would have to supply it.", BODY),
        ("", None),
        ("The checks are the point", H2),
        ("The balance sheet is proved to balance at both dates, the cash "
         "flow is proved to tie to the movement in cash, and reserves are "
         "proved to have moved by the profit. A statement that does not tie "
         "is wrong rather than untidy, and the workbook says so on its face "
         "instead of leaving you to find out.", BODY),
        ("", None),
        ("What is still only in the product", H2),
        ("Three things need more than a spreadsheet formula can carry and "
         "stay in the product: the Croston and Holt forecasts, the "
         "thirteen-week cash view, and the order sequencing that decides "
         "which purchase orders fit the cash. Everything else you are "
         "holding.", BODY),
        ("", None),
        ("Sheets", H2),
    ]
    r = 1
    for text, font in rows:
        if text:
            c = ws.cell(row=r, column=1, value=text)
            c.font = font or BODY
            c.alignment = Alignment(wrap_text=True, vertical="top")
        r += 1

    guide = [
        ("— read these first —", ""),
        ("Summary", "The whole business on one screen, worked out from "
                    "everything below."),
        ("Settings", "Every assumption the model makes, in one place. This "
                     "is the sheet to change."),
        ("Companies", "Your legal entities. The entity_id is the code every "
                      "other sheet uses."),
        ("Branches", "Warehouses and branches, each under a company."),
        ("— fill these in (white) —", ""),
        ("Inventory", "One row per item per branch. Stock on hand, cost, "
                      "lead time."),
        ("Demand", "One row per item per branch per month. A zero month is "
                   "information, not a gap."),
        ("Receivables", "Open invoices. Drives the ageing and the cash view."),
        ("Service", "Service jobs. Hours and parts, worked against billed."),
        ("Machines", "Placed instruments and what they have brought back."),
        ("Commitments", "Money already promised. Drives the cash view."),
        ("Trial balance", "Your accounts, with each one mapped to a "
                          "statement line. All three statements come from "
                          "this sheet."),
        ("Payroll", "One row per employee. Tax, EOBI, social security and "
                    "gratuity are worked out for you."),
        ("Pipeline", "Tenders and opportunities, weighted and checked "
                     "against the stock and the cash."),
        ("Marketing spend", "What it cost to get in front of customers."),
        ("— worked out for you (grey) —", ""),
        ("Profit and loss", "From the Trial balance. Nothing is entered."),
        ("Balance sheet", "From the Trial balance, at both dates, with the "
                          "proof that it balances."),
        ("Cash flow", "Derived from the other two statements, with the "
                      "proof that it ties to the movement in cash."),
        ("Stock policy", "The demand classification, the ABC ranking, "
                         "safety stock, reorder points and what to order."),
        ("Payroll calculation", "Every payslip worked out band by band."),
        ("Pipeline calculation", "Each deal weighted, and checked against "
                                 "the stock that would supply it."),
        ("Salary tax slabs", "The statutory slabs, dated and sourced. The "
                             "one grey sheet with figures typed in, because "
                             "they are the statute."),
        ("— the audit —", ""),
        ("Checks", "Every fault this workbook can see, counted."),
        ("Lists", "The dropdown values. No need to open it."),
    ]
    ws.cell(row=r, column=1, value="Sheet").font = HEADW
    ws.cell(row=r, column=2, value="What goes in it").font = HEADW
    r += 1
    for name, what in guide:
        if not what:
            # A divider, not a sheet. The three groups are the shape of the
            # workbook and are worth seeing before the list of names.
            h = ws.cell(row=r, column=1, value=name)
            h.font = Font(name=FONT, size=10, bold=True, italic=True,
                          color=MUTED)
            r += 1
            continue
        ws.cell(row=r, column=1, value=name).font = Font(
            name=FONT, size=10, bold=True, color=INK)
        c = ws.cell(row=r, column=2, value=what)
        c.font = BODY
        c.alignment = Alignment(wrap_text=True, vertical="top")
        r += 1

    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 96
    for i in range(1, r):
        ws.row_dimensions[i].height = None


def write_lists(ws):
    """Dropdown sources. Kept on its own sheet so the data sheets stay clean."""
    ws["A1"] = "yes_no"
    ws["A1"].font = HEADW
    for i, v in enumerate(["yes", "no"], start=2):
        ws.cell(row=i, column=1, value=v).font = BODY

    ws["D1"] = "commitment_kind"
    ws["D1"].font = HEADW
    kinds = ["lc_settlement", "loan_repayment", "payroll", "rent", "tax",
             "supplier", "other"]
    for i, v in enumerate(kinds, start=2):
        ws.cell(row=i, column=4, value=v).font = BODY

    ws["G1"] = "bucket"
    ws["G1"].font = HEADW
    for i, v in enumerate(["current", "1-30", "31-60", "61-90", "91-180",
                           "180+"], start=2):
        ws.cell(row=i, column=7, value=v).font = BODY

    ws["J1"] = "statement_line"
    ws["J1"].font = HEADW
    from engine.modules.finance import ALL_LINES
    for i, v in enumerate(ALL_LINES, start=2):
        ws.cell(row=i, column=10, value=v).font = BODY

    ws["M1"] = "stage"
    ws["N1"] = "odds"
    for cell in ("M1", "N1"):
        ws[cell].font = HEADW
    from engine.modules.marketing import DEFAULT_ODDS, STAGES
    for i, v in enumerate(STAGES, start=2):
        ws.cell(row=i, column=13, value=v).font = BODY
        odds = ws.cell(row=i, column=14,
                       value=1.0 if v == "won" else
                             0.0 if v == "lost" else DEFAULT_ODDS.get(v, 0.0))
        odds.font = BODY
        odds.number_format = PCT

    ws["P1"] = "spend_category"
    ws["P1"].font = HEADW
    for i, v in enumerate(("conference", "demo_unit", "samples", "digital",
                           "print", "other"), start=2):
        ws.cell(row=i, column=16, value=v).font = BODY

    for col in "ADGJMP":
        ws.column_dimensions[col].width = 24
    ws.sheet_state = "hidden"


def style_white(cell):
    cell.font = BODY
    cell.fill = PatternFill("solid", fgColor=WHITE)
    cell.border = BOX


def style_grey(cell):
    cell.font = Font(name=FONT, size=10, color=MUTED)
    cell.fill = PatternFill("solid", fgColor=GREY)
    cell.border = BOX


def write_header(ws, row, col, text, grey=False, note=""):
    c = ws.cell(row=row, column=col, value=text)
    c.font = HEADG if grey else HEADW
    c.fill = PatternFill("solid", fgColor=GREY_HEAD if grey else WHITE_HEAD)
    c.border = BOX
    c.alignment = Alignment(wrap_text=True, vertical="bottom")
    if note:
        # The note goes in the row above the header, because a cell comment is
        # invisible until hovered and these are the field definitions.
        n = ws.cell(row=row - 1, column=col, value=note)
        n.font = NOTE
        n.alignment = Alignment(wrap_text=True, vertical="top")
    return c


# The service-target matrix, read straight off the engine's own defaults so
# the workbook cannot quietly disagree with the product about them.
SERVICE_ABC = ("A", "B", "C")
SERVICE_CLASSES = ("smooth", "erratic", "intermittent", "lumpy")
SERVICE_TARGETS = Thresholds().service_targets
# What the engine falls back to for a pair not in the matrix, which in
# practice means a line with no demand history.
SERVICE_FALLBACK = 0.90


def write_settings(ws):
    ws.sheet_view.showGridLines = False
    ws["A1"] = "Setting"
    ws["B1"] = "Value"
    ws["C1"] = "What it changes"
    for c in ("A1", "B1", "C1"):
        ws[c].font = HEADW
        ws[c].fill = PatternFill("solid", fgColor=WHITE_HEAD)
        ws[c].border = BOX

    rows = [
        ("as_of_date", samples.AS_OF, DATE,
         "Every ageing and overdue figure in this workbook is measured from "
         "this date. The product asks for it separately."),
        ("target_cover_months", 3.0, NUM1,
         "Months of stock the policy aims to hold."),
        ("excess_cover_months", 6.0, NUM1,
         "Above this, stock is called excess and its carrying cost is "
         "counted against you."),
        ("cost_of_capital_annual", 0.22, PCT,
         "What money costs you a year. Prices both held stock and the "
         "overdue book."),
        ("target_dso_days", 60, "0",
         "The collection period you are aiming at."),
        ("labour_rate_per_hour", 2500.0, MONEY,
         "Prices unbilled service hours."),
        ("payback_target_months", 24, "0",
         "A placement should repay its capital within this."),
        ("min_transfer_value", 25000.0, MONEY,
         "Below this a transfer is not worth the paperwork."),
        ("sender_min_cover_months", 3.0, NUM1,
         "A branch never donates below this much of its own cover."),
        ("monthly_revenue", None, MONEY,
         "Needed for days-sales-outstanding. Leave blank if you would "
         "rather not state it: the ageing still works."),
        ("opening_balance", None, MONEY,
         "Bank balance at the as-of date. Without it the cash view shows "
         "movement rather than balances."),
        ("cash_buffer", 2000000.0, MONEY,
         "Cash the business will not spend below. This decides which "
         "purchase orders get placed."),
        ("collection_rate", 1.0, PCT,
         "The share of an overdue balance you expect to actually collect."),
        ("cash_horizon_weeks", 13, "0",
         "How many weeks the cash view covers."),
        ("forecast_horizon_months", 6, "0",
         "How far the forecast projects."),
        ("dividends_paid", 0.0, MONEY,
         "Paid out of reserves in the period. Without it the check that "
         "reserves moved by the profit fails for a business that has simply "
         "paid its owners."),
        ("minimum_wage_monthly", 43000.0, MONEY,
         "Provincial, and reset every year. EOBI is charged on this rather "
         "than on pay, so one wrong figure moves every employee at once. "
         "This is the 2026-27 Sindh notification."),
        ("social_security_ceiling", 0.0, MONEY,
         "Provinces insure workers up to a wage ceiling. Zero applies no "
         "ceiling, which overstates the cost rather than understating it."),
        ("gratuity_months_per_year", 1.0, NUM1,
         "One month's basic a year is the usual arrangement where there is "
         "no funded scheme."),
        # The policy cut-offs. They were literals inside the formulas until
        # they were pulled out here: a cut-off you cannot change is not a
        # setting, and every one of these is a judgement call a business is
        # entitled to make differently.
        ("clinical_critical_floor", 0.99, PCT,
         "A clinically critical line is never held below this, whatever its "
         "value class says. It is a floor, not an override: a line whose "
         "class already asks for more keeps the higher figure."),
        ("abc_a_cutoff", 0.80, PCT,
         "Lines making up this share of a branch's consumption value are A. "
         "The line that takes you past it is itself still an A."),
        ("abc_b_cutoff", 0.95, PCT,
         "Up to this share, B. Everything after is C."),
        ("adi_cutoff", 1.32, NUM1,
         "Above this average interval between orders the demand is treated "
         "as intermittent and an average stops meaning much. 1.32 is the "
         "Syntetos-Boylan figure."),
        ("cv_squared_cutoff", 0.49, NUM1,
         "Above this, order sizes are uneven enough to be called erratic or "
         "lumpy. 0.49 is the Syntetos-Boylan figure."),
        ("min_months_history", 6, "0",
         "Fewer months than this and the line is not classified at all. "
         "Policy figures are left blank rather than computed from too "
         "little to mean anything."),
        ("min_closed_for_measured_odds", 8, "0",
         "Until this many deals have closed, the pipeline uses the "
         "assumed odds on the Lists sheet. Past it, it uses your own "
         "measured win rate instead, and says which it used."),
        ("max_stage_odds", 0.95, PCT,
         "No stage is ever credited with better odds than this, however "
         "good the measured rate looks on a small number of deals."),
    ]
    at = {name: i for i, (name, *_r) in enumerate(rows, start=2)}
    for i, (name, value, fmt, why) in enumerate(rows, start=2):
        n = ws.cell(row=i, column=1, value=name)
        n.font = Font(name=FONT, size=10, bold=True, color=INK)
        n.border = BOX
        v = ws.cell(row=i, column=2, value=value)
        style_white(v)
        v.number_format = fmt
        w = ws.cell(row=i, column=3, value=why)
        w.font = NOTE
        w.alignment = Alignment(wrap_text=True, vertical="top")
        w.border = BOX

    last = len(rows) + 1

    # ---- the service-target matrix -------------------------------------
    #
    # A single A/B/C target is not what the product uses. The target depends
    # on BOTH the value class and how the demand behaves: a lumpy A line
    # cannot be held to the same standard as a steady one, because the
    # safety stock to do it would be absurd. Twelve cells, all editable,
    # because this is the main lever a business has over its stock.
    r = last + 2
    t = ws.cell(row=r, column=1, value="service_targets")
    t.font = Font(name=FONT, size=10, bold=True, color=INK)
    ws.cell(row=r, column=3, value="How often you intend to be able to "
            "supply, by value class and demand behaviour. Stock policy reads "
            "this matrix. A clinically critical line is floored at "
            "clinical_critical_floor below whatever it says here.").font = NOTE
    ws.cell(row=r, column=3).alignment = Alignment(wrap_text=True,
                                                   vertical="top")

    head = r + 1
    for j, cls_name in enumerate(SERVICE_CLASSES, start=2):
        c = ws.cell(row=head, column=j, value=cls_name)
        c.font = HEADW
        c.fill = PatternFill("solid", fgColor=WHITE_HEAD)
        c.border = BOX
    for i, abc in enumerate(SERVICE_ABC, start=head + 1):
        a = ws.cell(row=i, column=1, value=abc)
        a.font = Font(name=FONT, size=10, bold=True, color=INK)
        a.border = BOX
        for j, cls_name in enumerate(SERVICE_CLASSES, start=2):
            v = ws.cell(row=i, column=j,
                        value=SERVICE_TARGETS[(abc, cls_name)])
            style_white(v)
            v.number_format = PCT

    ws.cell(row=head + len(SERVICE_ABC) + 2, column=1,
            value="A line whose demand has no history falls back to "
                  "%.0f%%." % (SERVICE_FALLBACK * 100)).font = NOTE

    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 86
    return {
        "at": at,
        "first_class_col": get_column_letter(2),
        "last_class_col": get_column_letter(1 + len(SERVICE_CLASSES)),
        "class_row": head,
        "first_abc_row": head + 1,
        "last_abc_row": head + len(SERVICE_ABC),
    }


def write_reference_sheets(wb, entities, locations):
    """Companies and Branches. Everything else validates against these."""
    co = wb.create_sheet("Companies")
    co.sheet_view.showGridLines = False
    heads = ["entity_id", "legal_name", "ntn", "is_separate_company"]
    notes = ["The code every other sheet uses. Short, and never reused.",
             "As it appears on the invoice.",
             "Tax number. Optional.",
             "no if it is a division rather than a company in its own right."]
    for j, (h, note) in enumerate(zip(heads, notes), start=1):
        write_header(co, 3, j, h, note=note)
    for i, e in enumerate(entities, start=4):
        for j, v in enumerate([e, f"{e} (Pvt) Ltd", "", "yes"], start=1):
            style_white(co.cell(row=i, column=j, value=v))
    for i in range(4 + len(entities), 4 + len(entities) + 12):
        for j in range(1, len(heads) + 1):
            style_white(co.cell(row=i, column=j))
    for j, w in enumerate([14, 30, 16, 20], start=1):
        co.column_dimensions[get_column_letter(j)].width = w
    co.cell(row=1, column=1, value="Fill this in first. Every other sheet "
            "checks its company codes against this one.").font = NOTE

    br = wb.create_sheet("Branches")
    br.sheet_view.showGridLines = False
    heads = ["location_id", "entity_id", "name", "city", "is_warehouse"]
    notes = ["The branch code every other sheet uses.",
             "Which company it belongs to.",
             "As people say it out loud.",
             "Optional.",
             "no if it holds no stock. Transfers never suggest moving stock "
             "to a branch that holds none."]
    for j, (h, note) in enumerate(zip(heads, notes), start=1):
        write_header(br, 3, j, h, note=note)
    r = 4
    for ent, loc in locations:
        for j, v in enumerate([loc, ent, loc, "", "yes"], start=1):
            style_white(br.cell(row=r, column=j, value=v))
        g = br.cell(row=r, column=7,
                    value=f'=IF(COUNTIFS(Companies!$A:$A,B{r})=0,'
                          f'"company not set up","")')
        style_grey(g)
        g.number_format = "@"
        r += 1
    for i in range(r, r + 12):
        for j in range(1, len(heads) + 1):
            style_white(br.cell(row=i, column=j))
        g = br.cell(row=i, column=7,
                    value=f'=IF(B{i}="","",IF(COUNTIFS(Companies!$A:$A,B{i})=0,'
                          f'"company not set up",""))')
        style_grey(g)
        g.number_format = "@"
    write_header(br, 3, 7, "check_company_known", grey=True,
                 note="The company code is not on the Companies sheet.")
    for j, w in enumerate([14, 14, 26, 16, 16, 4, 26], start=1):
        br.column_dimensions[get_column_letter(j)].width = w
    br.cell(row=1, column=1, value="Fill this in second.").font = NOTE
    return co, br


def write_data_sheet(wb, key, rows, entities, locations):
    """One dataset: white columns exactly as the importer reads them, then the
    grey block of worked-out columns and checks."""
    spec = samples.DATASETS[key]
    ws = wb.create_sheet(sheet_title(key))
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A4"

    heads = [c.name for c in spec.columns]
    refs = col_refs(heads)

    ws.cell(row=1, column=1,
            value=f"{spec.filename} — white columns are the import, "
                  f"grey are worked out for you").font = NOTE

    for j, col in enumerate(spec.columns, start=1):
        note = (col.note or "")
        if col.required:
            note = "Required. " + note
        write_header(ws, 3, j, col.name, note=note)

    # the sample data, then blank rows so the sheet is usable as a form
    for i, row in enumerate(rows, start=4):
        for j, col in enumerate(spec.columns, start=1):
            raw = row.get(col.name, "")
            value = coerce(col.name, raw)
            c = ws.cell(row=i, column=j, value=value)
            style_white(c)
            c.number_format = number_format_for(col.name)
    last_data = 3 + len(rows)
    for i in range(last_data + 1, last_data + 1 + SPARE):
        for j, col in enumerate(spec.columns, start=1):
            c = ws.cell(row=i, column=j)
            style_white(c)
            c.number_format = number_format_for(col.name)

    # dropdowns, so a code can only be one that exists
    if "entity_id" in refs:
        dv = DataValidation(
            type="list", formula1="=Companies!$A$4:$A$40", allow_blank=True,
            errorTitle="Unknown company",
            error="Use a code from the Companies sheet.")
        ws.add_data_validation(dv)
        dv.add(f"{refs['entity_id']}4:{refs['entity_id']}{last_data + SPARE}")
    if "location_id" in refs:
        dv = DataValidation(
            type="list", formula1="=Branches!$A$4:$A$40", allow_blank=True,
            errorTitle="Unknown branch",
            error="Use a code from the Branches sheet.")
        ws.add_data_validation(dv)
        dv.add(f"{refs['location_id']}4:{refs['location_id']}{last_data + SPARE}")
    for name in heads:
        if name.startswith("is_"):
            dv = DataValidation(type="list", formula1='"yes,no"',
                                allow_blank=True, errorTitle="yes or no",
                                error="This column takes yes or no.")
            ws.add_data_validation(dv)
            dv.add(f"{refs[name]}4:{refs[name]}{last_data + SPARE}")
    CODED = {
        "commitments": ("kind", "=Lists!$D$2:$D$8"),
        "trial_balance": ("statement_line", "=Lists!$J$2:$J$22"),
        "opportunities": ("stage", "=Lists!$M$2:$M$7"),
        "marketing_spend": ("category", "=Lists!$P$2:$P$7"),
    }
    if key in CODED:
        column, source = CODED[key]
        if column in refs:
            dv = DataValidation(type="list", formula1=source, allow_blank=True,
                                errorTitle="Not one of the values",
                                error="Choose from the list.")
            ws.add_data_validation(dv)
            dv.add(f"{refs[column]}4:{refs[column]}{last_data + SPARE}")

    # the grey block
    short = SHORT.get(key, {})
    letters = {k: refs[v] for k, v in short.items() if v in refs}
    block = CALC.get(key, [])
    # Grey columns are referred to by name, %calc_days_overdue%, rather than by
    # a letter typed in by hand. A letter was wrong once already -- the ageing
    # bucket read the invoice amount instead of its age, and reported a
    # ninety-three day invoice as over a hundred and eighty -- and nothing
    # about the file looked wrong, because the formula evaluated cleanly.
    grey_letters = {header: get_column_letter(GREY_START + n)
                    for n, (header, *_rest) in enumerate(block)}
    guard = refs[ROW_KEY[key]]

    for n, (header, formula, fmt, note) in enumerate(block):
        col = GREY_START + n
        write_header(ws, 3, col, header, grey=True, note=note)
        body = formula.lstrip("=")
        for name, letter in grey_letters.items():
            body = body.replace(f"%{name}%", letter)
        assert "%" not in body, f"{key}.{header} refers to a grey column that does not exist"
        for i in range(4, last_data + SPARE + 1):
            # Guarded on the row having data, so a blank form row stays blank
            # rather than reporting every fault at once.
            expr = body.format(r=i, **letters)
            c = ws.cell(row=i, column=col,
                        value=f'=IF({guard}{i}="","",{expr})')
            style_grey(c)
            c.number_format = fmt
        ws.column_dimensions[get_column_letter(col)].width = max(
            14, min(26, len(header) + 4))

    for j, col in enumerate(spec.columns, start=1):
        ws.column_dimensions[get_column_letter(j)].width = max(
            11, min(24, len(col.name) + 3))
    ws.row_dimensions[2].height = 58
    return ws, last_data


def coerce(name, raw):
    """CSV text into something Excel will treat as a number or a date."""
    from datetime import date, datetime
    s = (raw or "").strip()
    if s == "":
        return None
    if name in DATE_COLUMNS or name in ("period",):
        if name == "period":
            return s                      # YYYY-MM is a label, not a date
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return s
    try:
        f = float(s)
        return int(f) if f.is_integer() and abs(f) < 1e15 else f
    except ValueError:
        return s


def number_format_for(name):
    if name in DATE_COLUMNS:
        return DATE
    if name in PCT_COLUMNS:
        return PCT
    if name in MONEY_COLUMNS:
        return MONEY
    if name in ("on_hand", "open_po", "quantity", "lead_time_days",
                "lead_time_days_std", "labour_hours", "labour_hours_billed",
                "contract_months", "expected_monthly_tests",
                "actual_monthly_tests", "downtime_days", "downtime_days_ytd",
                "recurs_every_days"):
        return QTY
    return "@" if name in ("period",) else "General"


def write_checks(ws, built, proofs):
    """Every fault this workbook can see, counted.

    This is the sheet to read before uploading. It counts the same faults the
    engine's own sanity checks would raise, so a bad export is caught here
    rather than after it has moved a figure on a dashboard.
    """
    ws.sheet_view.showGridLines = False
    ws["A1"] = "Before you upload"
    ws["A1"].font = H1
    ws["A2"] = ("Each row counts one kind of fault across one sheet. Anything "
                "other than zero is worth looking at before the figures are "
                "trusted; the ones marked 'makes a figure wrong' will be "
                "refused or will move a number.")
    ws["A2"].font = NOTE
    ws["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells("A2:E2")
    ws.row_dimensions[2].height = 32

    for j, h in enumerate(["Sheet", "What is checked", "How many", "Severity",
                           "What it does to the numbers"], start=1):
        c = ws.cell(row=4, column=j, value=h)
        c.font = HEADW
        c.fill = PatternFill("solid", fgColor=WHITE_HEAD)
        c.border = BOX

    SEVERITY = {
        "check_branch_known": "makes a figure wrong",
        "check_duplicate_line": "makes a figure wrong",
        "check_cost_blank": "makes a figure wrong",
        "check_lead_time": "makes a figure wrong",
        "check_item_known": "makes a figure wrong",
        "check_duplicate_period": "makes a figure wrong",
        "check_duplicate_invoice": "makes a figure wrong",
        "check_duplicate_job": "makes a figure wrong",
        "check_duplicate_machine": "makes a figure wrong",
        "check_margin_as_percent": "makes a figure wrong",
        "check_due_before_invoice": "makes a figure wrong",
        "check_closed_before_opened": "worth fixing",
        "check_recurs_without_period": "makes a figure wrong",
        "check_amount": "makes a figure wrong",
        "check_period_format": "makes a figure wrong",
        "check_negative": "worth fixing",
        "check_paid_over": "worth fixing",
        "check_customer_name": "worth fixing",
        "check_machine_known": "worth fixing",
        "check_billed_over_worked": "worth fixing",
        "check_expiry_before_cover": "worth fixing",
        "check_too_new_to_judge": "note",
        "check_kind": "note",
        "check_contract_months": "makes a figure wrong",
    }

    r = 5
    for key, (title, last) in built.items():
        for n, (header, _f, _fmt, note) in enumerate(CALC.get(key, [])):
            if not header.startswith("check_"):
                continue
            letter = get_column_letter(GREY_START + n)
            ws.cell(row=r, column=1, value=title).font = BODY
            ws.cell(row=r, column=2,
                    value=header.replace("check_", "").replace("_", " ")
                    ).font = BODY
            # The data range, not the whole column: counting the column
            # counted the header and the note above it, so every check read
            # exactly two on a clean workbook. A constant across unrelated
            # checks is the shape of a counting bug, not of bad data.
            c = ws.cell(
                row=r, column=3,
                value=f"=COUNTIF('{title}'!{letter}$4:{letter}${last},\"?*\")")
            c.font = Font(name=FONT, size=10, bold=True, color=INK)
            c.number_format = "0"
            c.border = BOX
            c.alignment = Alignment(horizontal="center")
            sev = SEVERITY.get(header, "worth fixing")
            s = ws.cell(row=r, column=4, value=sev)
            s.font = Font(name=FONT, size=9,
                          color=ALARM if sev == "makes a figure wrong" else MUTED)
            w = ws.cell(row=r, column=5, value=note)
            w.font = NOTE
            w.alignment = Alignment(wrap_text=True, vertical="top")
            for col in (1, 2, 4, 5):
                ws.cell(row=r, column=col).border = BOX
            r += 1

    # ---- the statements have to prove themselves too ------------------
    #
    # These are not data faults, they are arithmetic ones, and they are the
    # most serious kind: a balance sheet that does not balance or a cash
    # flow that does not tie means a figure on a dashboard is wrong, not
    # untidy. They live on their own sheets, and they are counted here as
    # well because this is the sheet people actually read before uploading.
    r += 1
    h = ws.cell(row=r, column=1, value="The statements")
    h.font = H2
    r += 1
    first_statement_row = r
    for ref, sheet, label, note in proofs:
        ws.cell(row=r, column=1, value=sheet).font = BODY
        ws.cell(row=r, column=2, value=label).font = BODY
        # One if the proof fails, nil if it holds, so these add into the
        # same total as every other fault on the sheet.
        c = ws.cell(row=r, column=3,
                    value=f"=IF(ROUND({ref},0)=0,0,1)")
        c.font = Font(name=FONT, size=10, bold=True, color=INK)
        c.number_format = "0"
        c.border = BOX
        c.alignment = Alignment(horizontal="center")
        sv = ws.cell(row=r, column=4, value="makes a figure wrong")
        sv.font = Font(name=FONT, size=9, color=ALARM)
        w = ws.cell(row=r, column=5, value=note)
        w.font = NOTE
        w.alignment = Alignment(wrap_text=True, vertical="top")
        for col in (1, 2, 4, 5):
            ws.cell(row=r, column=col).border = BOX
        r += 1

    total = ws.cell(row=r + 1, column=2, value="Faults found in total")
    total.font = H2
    t = ws.cell(row=r + 1, column=3, value=f"=SUM(C5:C{r - 1})")
    t.font = Font(name=FONT, size=11, bold=True, color=INK)
    t.number_format = "0"
    t.alignment = Alignment(horizontal="center")
    # Two totals, because they mean different things. A workbook with
    # nothing but notes against it is fine to upload; one with a single
    # 'makes a figure wrong' is not, and a combined count would have let
    # the second hide inside the first.
    serious = ws.cell(row=r + 2, column=2, value="Of those, serious")
    serious.font = Font(name=FONT, size=10, bold=True, color=INK)
    sc = ws.cell(row=r + 2, column=3,
                 value=f'=SUMIF(D5:D{r - 1},"makes a figure wrong",'
                       f'C5:C{r - 1})')
    sc.font = Font(name=FONT, size=11, bold=True, color=ALARM)
    sc.number_format = "0"
    sc.alignment = Alignment(horizontal="center")
    v = ws.cell(row=r + 4, column=2,
                value=f'=IF(C{r + 2}>0,'
                      f'"Fix the serious rows first: until they are nil a '
                      f'figure somewhere is wrong, not just untidy.",'
                      f'IF(C{r + 1}=0,"Nothing in this workbook contradicts '
                      f'anything else in it.",'
                      f'"Nothing serious. The rows above are worth a look '
                      f'but will not move a figure."))')
    v.font = Font(name=FONT, size=10, italic=True, color=MUTED)

    for j, w in enumerate([16, 30, 11, 22, 78], start=1):
        ws.column_dimensions[get_column_letter(j)].width = w
    return r


def sheet_to_csv(path, title):
    """A data sheet as the CSV the importer reads.

    Only the white columns: the grey ones are formulas, and openpyxl reads a
    formula as its text unless the file has been recalculated. Dates go back
    out as YYYY-MM-DD, and a number that is whole goes out without a decimal
    point, because that is what the templates look like.
    """
    import csv
    import io
    from datetime import date, datetime

    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    ws = wb[title]
    heads, keep = [], []
    for cell in ws[3]:
        name = cell.value
        if not name:
            continue
        if name.startswith(("calc_", "check_")):
            continue
        heads.append(name)
        keep.append(cell.column)

    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(heads)
    for row in ws.iter_rows(min_row=4, max_row=ws.max_row):
        values, any_value = [], False
        for col in keep:
            v = row[col - 1].value
            if v is None:
                values.append("")
                continue
            any_value = True
            if isinstance(v, datetime):
                values.append(v.date().isoformat())
            elif isinstance(v, date):
                values.append(v.isoformat())
            elif isinstance(v, float) and v.is_integer():
                values.append(str(int(v)))
            else:
                values.append(str(v))
        if any_value:
            w.writerow(values)
    return out.getvalue()


def main(out="sop-insight-data-model.xlsx"):
    import csv
    import io

    wb = Workbook()
    write_readme(wb.active)
    wb.active.title = "Read me"

    data = {}
    for key in samples.ORDER:
        text = samples.sample_csv(key)
        data[key] = list(csv.DictReader(io.StringIO(text)))

    entities = sorted({r["entity_id"] for r in data["inventory"]})
    locations = sorted({(r["entity_id"], r["location_id"])
                        for r in data["inventory"]})

    svc = write_settings(wb.create_sheet("Settings"))
    write_reference_sheets(wb, entities, locations)

    built = {}
    for key in samples.ORDER:
        _ws, last = write_data_sheet(wb, key, data[key], entities, locations)
        built[key] = (sheet_title(key), last + SPARE)

    write_lists(wb.create_sheet("Lists"))

    # The worked-out half. Lists is written before these because the slab
    # and stage tables they read live on it.
    slabs = write_tax_slabs(wb.create_sheet("Salary tax slabs"))
    stock = write_stock_calc(wb.create_sheet("Stock policy"),
                             len(data["inventory"]), svc)
    payroll = write_payroll_calc(wb.create_sheet("Payroll calculation"),
                                 slabs, len(data["employees"]))
    pipeline = write_pipeline_calc(wb.create_sheet("Pipeline calculation"),
                                   len(data["opportunities"]), svc)
    pnl = write_pnl(wb.create_sheet("Profit and loss"))
    bs = write_balance_sheet(wb.create_sheet("Balance sheet"))
    cf = write_cash_flow(wb.create_sheet("Cash flow"), pnl, bs)
    write_summary(wb.create_sheet("Summary"), pnl, bs, cf, stock, payroll,
                  pipeline)

    # Checks comes last because it reads the statements' own proof cells,
    # which do not exist until the statements have been written. The sheet
    # order below is fixed separately, so creation order does not show.
    write_checks(wb.create_sheet("Checks"), built, [
        (f"'Balance sheet'!B{bs['difference']}", "Balance sheet",
         "balance sheet balances at the start",
         "Assets must equal what is owed plus what is owned. If this is "
         "not nil, an entry has gone to one side of the books only."),
        (f"'Balance sheet'!C{bs['difference']}", "Balance sheet",
         "balance sheet balances at the end",
         "The same again at the period end. Balancing at the start and "
         "not at the end means something was posted during the period."),
        (f"'Cash flow'!B{cf['tie_difference']}", "Cash flow",
         "cash flow ties to the cash balance",
         "The statement's net movement must equal the change in cash on "
         "the balance sheet. If it does not, a working-capital movement "
         "has been missed and the statement is wrong, not untidy."),
        (f"'Cash flow'!B{cf['reserves_difference']}", "Cash flow",
         "reserves moved by the profit",
         "What the business earned should be what its reserves gained, "
         "once dividends are added back. This is the check that catches "
         "an entry posted to the wrong side."),
    ])

    # Read me, then what you fill in, then what it works out, then the
    # checks. Lists is hidden at the end.
    order = ["Read me", "Summary",
             "Settings", "Companies", "Branches",
             "Inventory", "Demand", "Receivables", "Service", "Machines",
             "Commitments", "Trial balance", "Payroll", "Pipeline",
             "Marketing spend",
             "Profit and loss", "Balance sheet", "Cash flow",
             "Stock policy", "Payroll calculation", "Pipeline calculation",
             "Salary tax slabs",
             "Checks", "Lists"]
    wb._sheets = [wb[name] for name in order]

    wb.save(out)
    print(f"wrote {out}")
    return out




# =========================================================== calculations
#
# From here on the sheets are worked out, not typed in. Every figure is a
# live formula off the input sheets, so changing a unit cost or a salary
# moves the statements without anybody re-running anything.
#
# What is NOT reproduced here, and why: the Croston and Holt forecasting,
# the thirteen-week cash engine, the transfer suggestions and the order
# sequencing. Each needs iteration or per-period state that a spreadsheet
# can only fake, and a faked version that disagrees with the product would
# be worse than an honest gap. The Summary sheet says so in those words.

TB = "'Trial balance'"
LINE_COL = f"{TB}!$D:$D"
OPEN_COL = f"{TB}!$E:$E"
CLOSE_COL = f"{TB}!$F:$F"


def tb_sum(line, which="closing"):
    col = CLOSE_COL if which == "closing" else OPEN_COL
    return f'SUMIFS({col},{LINE_COL},"{line}")'


def report_row(ws, r, label, formula, *, fmt=MONEY, bold=False, rule=False,
               indent=False, note=""):
    cell = ws.cell(row=r, column=1,
                   value=("    " if indent else "") + label)
    cell.font = Font(name=FONT, size=11 if bold else 10,
                     bold=bold, color=INK if bold else MUTED)
    v = ws.cell(row=r, column=2,
                value=formula if str(formula).startswith("=") else formula)
    v.font = Font(name=FONT, size=11 if bold else 10, bold=bold, color=INK)
    v.number_format = fmt
    v.fill = PatternFill("solid", fgColor=GREY)
    v.alignment = Alignment(horizontal="right")
    if rule:
        for c in (cell, v):
            c.border = Border(top=thin)
    if note:
        n = ws.cell(row=r, column=3, value=note)
        n.font = NOTE
        n.alignment = Alignment(wrap_text=True, vertical="top")
    return r + 1


def sheet_intro(ws, title, lines):
    ws.sheet_view.showGridLines = False
    ws["A1"] = title
    ws["A1"].font = H1
    r = 2
    for line in lines:
        c = ws.cell(row=r, column=1, value=line)
        c.font = NOTE
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        ws.row_dimensions[r].height = 15 * (1 + len(line) // 110)
        r += 1
    return r + 1


def write_pnl(ws):
    r = sheet_intro(ws, "Profit and loss", [
        "Worked out from the Trial balance sheet. Nothing here is typed in: "
        "change a closing balance there and every figure below moves.",
        "Each account lands on the line its statement_line column says, "
        "never one guessed from its name.",
    ])
    head = r
    ws.cell(row=r, column=1, value="For the period ending").font = HEADW
    ws.cell(row=r, column=2, value="=Settings!$B$2").font = HEADW
    ws.cell(row=r, column=2).number_format = DATE
    r += 2

    r = report_row(ws, r, "Revenue", f"={tb_sum('revenue')}")
    r = report_row(ws, r, "Cost of sales", f"=-{tb_sum('cogs')}")
    r = report_row(ws, r, "Gross profit", f"=B{r-2}+B{r-1}", bold=True,
                   rule=True, note="Revenue less what it cost to buy.")
    gross = r - 1
    r = report_row(ws, r, "Gross margin", f"=IFERROR(B{gross}/B{gross-2},0)",
                   fmt=PCT, indent=True)
    r += 1
    r = report_row(ws, r, "Payroll", f"=-{tb_sum('payroll')}", indent=True)
    r = report_row(ws, r, "Other overheads", f"=-{tb_sum('opex')}", indent=True)
    r = report_row(ws, r, "Depreciation", f"=-{tb_sum('depreciation')}",
                   indent=True)
    dep = r - 1
    r = report_row(ws, r, "Operating profit",
                   f"=B{gross}+B{r-3}+B{r-2}+B{r-1}", bold=True, rule=True,
                   note="What the business made before financing and tax.")
    op = r - 1
    r = report_row(ws, r, "Finance cost", f"=-{tb_sum('finance_cost')}",
                   indent=True)
    r = report_row(ws, r, "Other income", f"={tb_sum('other_income')}",
                   indent=True)
    r = report_row(ws, r, "Profit before tax", f"=B{op}+B{r-2}+B{r-1}",
                   bold=True, rule=True)
    pbt = r - 1
    r = report_row(ws, r, "Tax", f"=-{tb_sum('tax')}", indent=True)
    r = report_row(ws, r, "Profit after tax", f"=B{pbt}+B{r-1}", bold=True,
                   rule=True, note="What is left, and what reserves should "
                                   "have moved by.")
    pat = r - 1
    r = report_row(ws, r, "Net margin", f"=IFERROR(B{pat}/B{gross-2},0)",
                   fmt=PCT, indent=True)

    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 70
    return {"gross": gross, "operating": op, "pbt": pbt, "pat": pat,
            "depreciation": dep, "revenue": gross - 2}


def write_balance_sheet(ws):
    r = sheet_intro(ws, "Balance sheet", [
        "Worked out from the Trial balance sheet, at the opening and the "
        "closing date.",
        "The check at the bottom is the point: a balance sheet that does not "
        "balance is not a presentational problem, it means something was "
        "posted to one side only.",
    ])
    ws.cell(row=r, column=1, value="").font = HEADW
    for j, label in ((2, "Opening"), (3, "Closing"), (4, "Movement")):
        c = ws.cell(row=r, column=j, value=label)
        c.font = HEADW
        c.alignment = Alignment(horizontal="right")
    r += 1

    def line(label, key, *, bold=False, rule=False, indent=False, negate=False):
        nonlocal r
        sign = "-" if negate else ""
        a = ws.cell(row=r, column=1, value=("    " if indent else "") + label)
        a.font = Font(name=FONT, size=11 if bold else 10, bold=bold,
                      color=INK if bold else MUTED)
        for j, which in ((2, "opening"), (3, "closing")):
            v = ws.cell(row=r, column=j,
                        value=f"={sign}{tb_sum(key, which)}")
            v.font = Font(name=FONT, size=10, bold=bold, color=INK)
            v.number_format = MONEY
            v.fill = PatternFill("solid", fgColor=GREY)
        m = ws.cell(row=r, column=4, value=f"=C{r}-B{r}")
        m.font = Font(name=FONT, size=10, color=MUTED)
        m.number_format = MONEY
        m.fill = PatternFill("solid", fgColor=GREY)
        if rule:
            for j in range(1, 5):
                ws.cell(row=r, column=j).border = Border(top=thin)
        r += 1
        return r - 1

    def total(label, rows, *, note=""):
        nonlocal r
        a = ws.cell(row=r, column=1, value=label)
        a.font = Font(name=FONT, size=11, bold=True, color=INK)
        for j in (2, 3, 4):
            col = get_column_letter(j)
            v = ws.cell(row=r, column=j,
                        value="=" + "+".join(f"{col}{x}" for x in rows))
            v.font = Font(name=FONT, size=11, bold=True, color=INK)
            v.number_format = MONEY
            v.fill = PatternFill("solid", fgColor=GREY)
            v.border = Border(top=thin)
        ws.cell(row=r, column=1).border = Border(top=thin)
        if note:
            n = ws.cell(row=r, column=5, value=note)
            n.font = NOTE
        r += 1
        return r - 1

    ws.cell(row=r, column=1, value="ASSETS").font = HEADG
    r += 1
    asset_rows = [
        line("Cash and bank", "cash", indent=True),
        line("Trade debtors", "receivables", indent=True),
        line("Stock", "inventory", indent=True),
        line("Other current assets", "other_current_assets", indent=True),
        line("Fixed assets, at cost", "fixed_assets", indent=True),
        line("Accumulated depreciation", "accumulated_depreciation",
             indent=True, negate=True),
    ]
    assets = total("Total assets", asset_rows)
    r += 1

    ws.cell(row=r, column=1, value="LIABILITIES").font = HEADG
    r += 1
    liab_rows = [
        line("Trade creditors", "payables", indent=True),
        line("Short-term debt", "short_term_debt", indent=True),
        line("Long-term debt", "long_term_debt", indent=True),
        line("Tax payable", "tax_payable", indent=True),
        line("Other liabilities", "other_liabilities", indent=True),
    ]
    liabilities = total("Total liabilities", liab_rows)
    r += 1

    ws.cell(row=r, column=1, value="EQUITY").font = HEADG
    r += 1
    eq_rows = [
        line("Share capital", "equity", indent=True),
        line("Retained earnings", "retained_earnings", indent=True),
    ]
    equity = total("Total equity", eq_rows)
    r += 2

    ws.cell(row=r, column=1, value="DOES IT BALANCE?").font = HEADG
    r += 1
    for j, which in ((2, "Opening"), (3, "Closing")):
        pass
    diff_row = r
    a = ws.cell(row=r, column=1, value="Assets less liabilities and equity")
    a.font = Font(name=FONT, size=10, color=MUTED)
    for j in (2, 3):
        col = get_column_letter(j)
        v = ws.cell(row=r, column=j,
                    value=f"={col}{assets}-{col}{liabilities}-{col}{equity}")
        v.font = Font(name=FONT, size=10, color=INK)
        v.number_format = MONEY
        v.fill = PatternFill("solid", fgColor=GREY)
    r += 1
    verdict = ws.cell(
        row=r, column=1,
        value=f'=IF(AND(ABS(B{diff_row})<1,ABS(C{diff_row})<1),'
              f'"It balances at both dates.",'
              f'"It does not balance. Something was posted to one side only; '
              f'every figure above is suspect until it is found.")')
    verdict.font = Font(name=FONT, size=10, italic=True, color=INK)
    r += 2

    # the ratios an owner actually looks at
    ca = f"(C{asset_rows[0]}+C{asset_rows[1]}+C{asset_rows[2]}+C{asset_rows[3]})"
    cl = f"(C{liab_rows[0]}+C{liab_rows[1]}+C{liab_rows[3]}+C{liab_rows[4]})"
    debt = f"(C{liab_rows[1]}+C{liab_rows[2]})"
    ws.cell(row=r, column=1, value="WHAT IT SAYS").font = HEADG
    r += 1
    r = report_row(ws, r, "Working capital", f"={ca}-{cl}",
                   note="Current assets less current liabilities. What is "
                        "left to run on.")
    r = report_row(ws, r, "Current ratio", f"=IFERROR({ca}/{cl},0)",
                   fmt="0.00",
                   note="Above 1 means the short-term bills are covered.")
    r = report_row(ws, r, "Gearing",
                   f"=IFERROR({debt}/({debt}+C{equity}),0)", fmt=PCT,
                   note="Debt against debt plus equity.")

    ws.column_dimensions["A"].width = 32
    for col in "BCD":
        ws.column_dimensions[col].width = 16
    ws.column_dimensions["E"].width = 60
    return {"assets": assets, "liabilities": liabilities, "equity": equity,
            "cash": asset_rows[0], "receivables": asset_rows[1],
            "inventory": asset_rows[2], "other_ca": asset_rows[3],
            "fixed": asset_rows[4], "payables": liab_rows[0],
            "std": liab_rows[1], "ltd": liab_rows[2],
            "tax_payable": liab_rows[3], "other_liab": liab_rows[4],
            "capital": eq_rows[0], "reserves": eq_rows[1],
            "difference": diff_row}


def write_cash_flow(ws, pnl, bs):
    r = sheet_intro(ws, "Cash flow", [
        "Derived, not entered. It starts at the profit, adds back what never "
        "moved cash, and then undoes everything the balance sheet absorbed.",
        "The check at the bottom is what makes it worth reading: the net "
        "movement must equal the change in cash on the balance sheet. Where "
        "it does not, a working-capital movement has been missed and the "
        "statement is wrong, not untidy.",
    ])
    P, B = "'Profit and loss'", "'Balance sheet'"

    ws.cell(row=r, column=1, value="CASH FROM OPERATIONS").font = HEADG
    r += 1
    r = report_row(ws, r, "Profit after tax", f"={P}!$B${pnl['pat']}",
                   indent=True)
    r = report_row(ws, r, "Depreciation", f"=-{P}!$B${pnl['depreciation']}",
                   indent=True,
                   note="Added back: it reduced the profit but no money left.")
    r = report_row(ws, r, "Movement in receivables",
                   f"=-{B}!$D${bs['receivables']}", indent=True,
                   note="More owed to you is profit you have not collected.")
    r = report_row(ws, r, "Movement in stock",
                   f"=-{B}!$D${bs['inventory']}", indent=True)
    r = report_row(ws, r, "Movement in other current assets",
                   f"=-{B}!$D${bs['other_ca']}", indent=True)
    r = report_row(ws, r, "Movement in payables",
                   f"={B}!$D${bs['payables']}", indent=True,
                   note="More owed by you is cash you are still holding.")
    r = report_row(ws, r, "Movement in tax payable",
                   f"={B}!$D${bs['tax_payable']}", indent=True)
    r = report_row(ws, r, "Movement in other liabilities",
                   f"={B}!$D${bs['other_liab']}", indent=True)
    r = report_row(ws, r, "Cash from operations",
                   f"=SUM(B{r-8}:B{r-1})", bold=True, rule=True)
    cfo = r - 1
    r += 1

    ws.cell(row=r, column=1, value="CASH FROM INVESTING").font = HEADG
    r += 1
    r = report_row(ws, r, "Fixed assets bought, at cost",
                   f"=-{B}!$D${bs['fixed']}", indent=True,
                   note="At cost, so depreciation is not counted twice.")
    r = report_row(ws, r, "Cash from investing", f"=B{r-1}", bold=True,
                   rule=True)
    cfi = r - 1
    r += 1

    ws.cell(row=r, column=1, value="CASH FROM FINANCING").font = HEADG
    r += 1
    r = report_row(ws, r, "Short-term borrowing", f"={B}!$D${bs['std']}",
                   indent=True)
    r = report_row(ws, r, "Long-term borrowing", f"={B}!$D${bs['ltd']}",
                   indent=True)
    r = report_row(ws, r, "Capital introduced", f"={B}!$D${bs['capital']}",
                   indent=True)
    r = report_row(ws, r, "Dividends paid", "=-Settings!$B$17", indent=True,
                   note="From Settings. A dividend is why reserves can move "
                        "by less than the profit.")
    r = report_row(ws, r, "Cash from financing", f"=SUM(B{r-4}:B{r-1})",
                   bold=True, rule=True)
    cff = r - 1
    r += 1

    r = report_row(ws, r, "Net movement in cash",
                   f"=B{cfo}+B{cfi}+B{cff}", bold=True, rule=True)
    net = r - 1
    r += 1

    ws.cell(row=r, column=1, value="DOES IT TIE?").font = HEADG
    r += 1
    r = report_row(ws, r, "Cash at the start", f"={B}!$B${bs['cash']}",
                   indent=True)
    r = report_row(ws, r, "Cash at the end", f"={B}!$C${bs['cash']}",
                   indent=True)
    r = report_row(ws, r, "Movement on the balance sheet",
                   f"=B{r-1}-B{r-2}", indent=True)
    on_bs = r - 1
    r = report_row(ws, r, "This statement says", f"=B{net}", indent=True)
    r = report_row(ws, r, "Difference", f"=B{r-1}-B{on_bs}", bold=True,
                   rule=True)
    diff = r - 1
    verdict = ws.cell(
        row=r, column=1,
        value=f'=IF(ABS(B{diff})<1,"It ties. The statement agrees with the '
              f'balance sheet.","It does not tie. A working-capital movement '
              f'has been missed: the statement is wrong, not untidy.")')
    verdict.font = Font(name=FONT, size=10, italic=True, color=INK)
    r += 2

    r = report_row(ws, r, "Reserves moved by",
                   f"={B}!$D${bs['reserves']}", indent=True)
    r = report_row(ws, r, "Profit after tax was",
                   f"={P}!$B${pnl['pat']}", indent=True)
    r = report_row(ws, r, "Difference, after dividends",
                   f"=B{r-2}+Settings!$B$17-B{r-1}", bold=True, rule=True,
                   note="Should be nil. Anything else is an entry posted to "
                        "the wrong side.")
    re_diff = r - 1
    v2 = ws.cell(
        row=r, column=1,
        value=f'=IF(ABS(B{re_diff})<1,"Reserves moved by the profit, as they '
              f'should.","Reserves did not move by the profit. Look for an '
              f'entry posted somewhere other than the profit and loss.")')
    v2.font = Font(name=FONT, size=10, italic=True, color=INK)

    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 64
    return {"cfo": cfo, "cfi": cfi, "cff": cff, "net": net,
            "tie_difference": diff, "reserves_difference": re_diff}


def write_tax_slabs(ws):
    """The slabs, on a sheet of their own.

    On a sheet rather than buried in a formula because they change with
    every Finance Act, and because an accountant checking a payslip wants
    to see the band it used, not trust that somebody typed it correctly.
    """
    from engine.payroll import TY2027

    r = sheet_intro(ws, "Salary tax slabs", [
        TY2027.name + ".",
        TY2027.source,
        "Tax on an annual taxable salary is the fixed amount for its band "
        "plus the rate on whatever is above that band's floor. When the next "
        "Finance Act lands, change these three columns and every payslip in "
        "the workbook moves with them.",
    ])
    for j, label in enumerate(("Band floor", "Fixed amount", "Rate above it"),
                              start=1):
        c = ws.cell(row=r, column=j, value=label)
        c.font = HEADW
        c.fill = PatternFill("solid", fgColor=WHITE_HEAD)
        c.border = BOX
    first = r + 1
    r += 1
    for band in TY2027.bands:
        for j, (value, fmt) in enumerate((
                (band.floor, MONEY), (band.fixed, MONEY), (band.rate, PCT)),
                start=1):
            c = ws.cell(row=r, column=j, value=value)
            style_white(c)
            c.number_format = fmt
        r += 1
    last = r - 1

    r += 1
    note = ws.cell(row=r, column=1, value=(
        "The 9% surcharge on salary above PKR 10,000,000 was abolished for "
        "salaried individuals by the Finance Act 2026. It still applies to "
        "non-salaried individuals and to associations of persons, neither of "
        "which this workbook computes."))
    note.font = NOTE
    note.alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=r, start_column=1, end_row=r + 1, end_column=4)

    for col, width in zip("ABCD", (18, 18, 16, 20)):
        ws.column_dimensions[col].width = width
    return {"first": first, "last": last}


PAYROLL_CALC = [
    ("Gross a month", "=Payroll!{bm}{r}+Payroll!{am}{r}+Payroll!{mm}{r}",
     MONEY, "Basic plus allowances plus medical."),
    ("Medical exempt", "=MIN(Payroll!{mm}{r},Payroll!{bm}{r}*0.1)", MONEY,
     "Exempt up to a tenth of basic; the rest is taxable."),
    ("Taxable a month",
     "=Payroll!{bm}{r}+Payroll!{am}{r}+Payroll!{mm}{r}-%Medical exempt%",
     MONEY, "What the slabs are applied to, monthly."),
    ("Taxable a year", "=%Taxable a month%*12", MONEY, ""),
    ("Band floor",
     "=LOOKUP(%Taxable a year%,'Salary tax slabs'!$A${first}:$A${last},"
     "'Salary tax slabs'!$A${first}:$A${last})", MONEY,
     "Which band this salary falls in."),
    ("Band fixed",
     "=LOOKUP(%Taxable a year%,'Salary tax slabs'!$A${first}:$A${last},"
     "'Salary tax slabs'!$B${first}:$B${last})", MONEY, ""),
    ("Band rate",
     "=LOOKUP(%Taxable a year%,'Salary tax slabs'!$A${first}:$A${last},"
     "'Salary tax slabs'!$C${first}:$C${last})", PCT, ""),
    ("Tax a year",
     "=%Band fixed%+(%Taxable a year%-%Band floor%)*%Band rate%",
     MONEY, "The slab calculation, written out."),
    ("Tax a month", "=%Tax a year%/12", MONEY,
     "Withheld as a twelfth, which is how it is deducted in practice."),
    ("Effective rate", "=IFERROR(%Tax a year%/%Taxable a year%,0)", PCT,
     "Tax against taxable pay. Always below the band rate."),
    ("EOBI employee",
     '=IF(LOWER(Payroll!{er}{r})="yes",Settings!$B$18*0.01,0)', MONEY,
     "1% of the minimum wage, not of this salary."),
    ("EOBI employer",
     '=IF(LOWER(Payroll!{er}{r})="yes",Settings!$B$18*0.05,0)', MONEY,
     "5% of the minimum wage. The clerk and the manager cost the same."),
    ("Social security",
     '=IF(AND(LOWER(Payroll!{ss}{r})="yes",OR(Settings!$B$19<=0,'
     '%Gross a month%<=Settings!$B$19)),%Gross a month%*0.06,0)',
     MONEY, "6% of pay, employer only, where the worker is insured and "
            "under any provincial ceiling."),
    ("Provident fund employee", "=Payroll!{bm}{r}*Payroll!{qp}{r}", MONEY, ""),
    ("Provident fund employer", "=Payroll!{bm}{r}*Payroll!{ep}{r}", MONEY, ""),
    ("Gratuity accrued",
     "=Payroll!{bm}{r}*Settings!$B$20/12", MONEY,
     "Accrued monthly so the cost lands in the month it was earned."),
    ("Net pay",
     "=%Gross a month%-%Tax a month%-%EOBI employee%"
     "-%Provident fund employee%", MONEY,
     "What reaches the bank account."),
    ("Cost to employer",
     "=%Gross a month%+%EOBI employer%+%Social security%"
     "+%Provident fund employer%+%Gratuity accrued%", MONEY,
     "What the business actually pays out for this person."),
]


def write_payroll_calc(ws, slabs, staff_rows):
    r = sheet_intro(ws, "Payroll calculation", [
        "One row per person on the Payroll sheet, worked out in full. Change "
        "a salary there and everything here moves.",
        "Tax is the slab calculation written out step by step rather than "
        "hidden in one formula, so a payslip can be checked by eye against "
        "the Salary tax slabs sheet.",
        "Not modelled: bonuses, arrears, final settlements and tax credits. "
        "Those need facts this workbook does not hold.",
    ])

    heads = ["Employee", "Name", "Department"] + [h for h, *_ in PAYROLL_CALC]
    for j, h in enumerate(heads, start=1):
        c = ws.cell(row=r, column=j, value=h)
        c.font = HEADG if j > 3 else HEADW
        c.fill = PatternFill("solid", fgColor=GREY_HEAD if j > 3 else WHITE_HEAD)
        c.border = BOX
        c.alignment = Alignment(wrap_text=True, vertical="bottom")
        if j > 3:
            n = ws.cell(row=r - 1, column=j,
                        value=PAYROLL_CALC[j - 4][3])
            n.font = NOTE
            n.alignment = Alignment(wrap_text=True, vertical="top")
    header = r
    ws.row_dimensions[r - 1].height = 46
    r += 1
    first_row = r

    letters = {h: get_column_letter(4 + i)
               for i, (h, *_rest) in enumerate(PAYROLL_CALC)}
    # the Payroll input sheet's own columns
    pay_spec = [c.name for c in samples.DATASETS["employees"].columns]
    pay = {k: get_column_letter(pay_spec.index(v) + 1) for k, v in {
        "bm": "basic_monthly", "am": "allowances_monthly",
        "mm": "medical_monthly", "ep": "employer_pf_rate",
        "qp": "employee_pf_rate", "er": "eobi_registered",
        "ss": "social_security",
    }.items()}

    for i in range(staff_rows + SPARE):
        src = FIRST_DATA_ROW + i
        for j, (head, formula, fmt, _note) in enumerate(PAYROLL_CALC, start=4):
            body = formula
            for name, letter in letters.items():
                body = body.replace(f"%%{name}%%", letter)
            for name, letter in letters.items():
                body = body.replace(f"%{name}%", f"{letter}{r}")
            assert "%" not in body, head
            expr = body.format(r=src, first=slabs["first"],
                               last=slabs["last"], **pay)
            c = ws.cell(row=r, column=j,
                        value=f'=IF(Payroll!$A${src}="","",{expr[1:]})')
            style_grey(c)
            c.number_format = fmt
        for j, col in enumerate(("A", "B", "E"), start=1):
            c = ws.cell(row=r, column=j,
                        value=f'=IF(Payroll!${col}${src}="","",'
                              f'Payroll!${col}${src})')
            style_grey(c)
            c.number_format = "@"
        r += 1
    last_row = r - 1

    # totals, which are what the profit and loss wants
    t = r + 1
    ws.cell(row=t, column=3, value="Totals").font = H2
    for j, (head, _f, fmt, _n) in enumerate(PAYROLL_CALC, start=4):
        col = get_column_letter(j)
        if fmt == PCT:
            continue
        c = ws.cell(row=t, column=j,
                    value=f"=SUM({col}{first_row}:{col}{last_row})")
        c.font = Font(name=FONT, size=10, bold=True, color=INK)
        c.number_format = fmt
        c.border = Border(top=thin)

    ws.freeze_panes = ws.cell(row=header + 1, column=4)
    for j in range(1, len(heads) + 1):
        ws.column_dimensions[get_column_letter(j)].width = 15 if j > 3 else 20
    return {"header": header, "first": first_row, "last": last_row,
            "totals": t, "cols": letters}


STOCK_CALC = [
    ("Months of history",
     '=COUNTIFS(Demand!$C:$C,Inventory!{ik}{r},Demand!$A:$A,Inventory!{en}{r},'
     'Demand!$B:$B,Inventory!{lo}{r})', "0",
     "How many months of demand this line has. Under six and a forecast is "
     "a guess; under three there is no forecast at all."),
    ("Months with demand",
     '=COUNTIFS(Demand!$C:$C,Inventory!{ik}{r},Demand!$A:$A,Inventory!{en}{r},'
     'Demand!$B:$B,Inventory!{lo}{r},Demand!$E:$E,">0")', "0",
     "Months that were not zero. The gap between this and the last column "
     "is what makes demand intermittent."),
    ("Mean monthly demand",
     '=IFERROR(AVERAGEIFS(Demand!$E:$E,Demand!$C:$C,Inventory!{ik}{r},'
     'Demand!$A:$A,Inventory!{en}{r},Demand!$B:$B,Inventory!{lo}{r}),0)',
     NUM1, "Average units a month over the history held."),
    ("Demand variability",
     # The ranges MUST start at the first data row, not row 2. Rows above it
     # hold the header and the note text, and subtracting the mean from text
     # raises #VALUE! inside the array, which the IFERROR then swallows into a
     # silent zero -- which in turn zeroes CV squared, the demand class and
     # every safety stock below. A clean recalc does not catch it.
     '=IFERROR(SQRT(SUMPRODUCT((Demand!$C$%(d)s:$C$5000=Inventory!{ik}{r})*'
     '(Demand!$A$%(d)s:$A$5000=Inventory!{en}{r})*'
     '(Demand!$B$%(d)s:$B$5000=Inventory!{lo}{r})*'
     '(Demand!$E$%(d)s:$E$5000-%%Mean monthly demand%%)^2)/'
     'MAX(1,%%Months of history%%-1)),0)' % {"d": FIRST_DATA_ROW}, NUM1,
     "Standard deviation of monthly demand. This is what safety stock is "
     "really protecting against."),
    ("Mean nonzero size",
     '=IFERROR(AVERAGEIFS(Demand!$E:$E,Demand!$C:$C,Inventory!{ik}{r},'
     'Demand!$A:$A,Inventory!{en}{r},Demand!$B:$B,Inventory!{lo}{r},'
     'Demand!$E:$E,">0"),0)', NUM1,
     "Average size of an order WHEN demand arrives, ignoring the empty "
     "months. CV squared is measured on these sizes, not on the whole "
     "series: ADI already carries the intermittency, and counting the "
     "zeros again would double-count it."),
    ("Nonzero size variability",
     # Ranges start at the first data row for the same reason as above.
     '=IFERROR(SQRT(SUMPRODUCT((Demand!$C$%(d)s:$C$5000=Inventory!{ik}{r})*'
     '(Demand!$A$%(d)s:$A$5000=Inventory!{en}{r})*'
     '(Demand!$B$%(d)s:$B$5000=Inventory!{lo}{r})*'
     '(Demand!$E$%(d)s:$E$5000>0)*'
     '(Demand!$E$%(d)s:$E$5000-%%Mean nonzero size%%)^2)/'
     'MAX(1,%%Months with demand%%-1)),0)' % {"d": FIRST_DATA_ROW}, NUM1,
     "Standard deviation of those order sizes."),
    ("ADI",
     '=IFERROR(%Months of history%/%Months with demand%,0)', "0.00",
     "Average interval between demands. Above 1.32 the demand is "
     "intermittent and an average is the wrong tool."),
    ("CV squared",
     # On the NON-ZERO sizes, which is what Syntetos-Boylan measures and
     # what the engine computes. Taken over the whole series instead, every
     # intermittent item reads as lumpy.
     '=IF(%Months with demand%<2,"",'
     'IFERROR((%Nonzero size variability%/%Mean nonzero size%)^2,""))',
     "0.00", "How uneven the sizes are when demand does arrive. Blank "
     "where one month or none had any demand, because a single order "
     "size has no variability to measure."),
    ("Demand class",
     # Six months is the engine's minimum: under it nothing is classified.
     # With fewer than two non-zero months CV squared is unknown, so ADI
     # decides alone rather than a blank being read as zero.
     '=IF(OR(%Months of history%<Settings!$B${s_minhist},'
     '%Months with demand%=0),"no history",'
     'IF(%CV squared%="",IF(%ADI%>=Settings!$B${s_adi},"intermittent",'
     '"smooth"),'
     'IF(AND(%ADI%<Settings!$B${s_adi},'
     '%CV squared%<Settings!$B${s_cv2}),"smooth",'
     'IF(AND(%ADI%>=Settings!$B${s_adi},'
     '%CV squared%<Settings!$B${s_cv2}),"intermittent",'
     'IF(AND(%ADI%<Settings!$B${s_adi},'
     '%CV squared%>=Settings!$B${s_cv2}),"erratic","lumpy")))))', "@",
     "Syntetos-Boylan classification. It decides which forecasting method "
     "the product uses, and whether an average means anything at all."),
    ("Annual consumption value",
     '=%Mean monthly demand%*12*Inventory!{uc}{r}', MONEY,
     "What this line consumes in a year, at cost. The basis for the ABC "
     "ranking."),
    ("ABC class",
     # Ranked WITHIN the branch, which is what the engine does: the class
     # drives this branch's service target, so the comparison set is the
     # lines this branch carries, not every line in the company. The two
     # criteria columns live on the Inventory sheet because this sheet's
     # rows line up with it one for one.
     #
     # Share-before is the value of every line at this branch worth MORE
     # than this one, over the branch total. That is the usual convention:
     # the line that takes you past 80% is itself still an A. Two lines at
     # one branch with exactly equal value are treated as a single step, so
     # a tie that straddles a cutoff puts both on the higher side.
     '=IF(%Annual consumption value%=0,"C",'
     'IF(IFERROR(SUMIFS($%%Annual consumption value%%:'
     '$%%Annual consumption value%%,'
     '$%%Annual consumption value%%:$%%Annual consumption value%%,'
     '">"&%Annual consumption value%,'
     '$C:$C,$C{r2c},$B:$B,$B{r2c})'
     '/SUMIFS($%%Annual consumption value%%:'
     '$%%Annual consumption value%%,'
     '$C:$C,$C{r2c},$B:$B,$B{r2c}),1)<Settings!$B${s_abca},"A",'
     'IF(IFERROR(SUMIFS($%%Annual consumption value%%:'
     '$%%Annual consumption value%%,'
     '$%%Annual consumption value%%:$%%Annual consumption value%%,'
     '">"&%Annual consumption value%,'
     '$C:$C,$C{r2c},$B:$B,$B{r2c})'
     '/SUMIFS($%%Annual consumption value%%:'
     '$%%Annual consumption value%%,'
     '$C:$C,$C{r2c},$B:$B,$B{r2c}),1)<Settings!$B${s_abcb},'
     '"B","C")))', "@",
     "A is the top 80% of this branch's consumption value, B the next 15%, "
     "C the rest. Ranked per branch, not company-wide."),
    ("Service target",
     # The engine reads a 12-cell matrix on Settings: the target depends on
     # the value class AND on how the demand behaves. A clinically critical
     # line is FLOORED here, not overridden -- a line whose class already
     # asks for more keeps the higher figure, which is what MAX does and
     # what a nested IF got wrong.
     '=IFERROR(MAX('
     'IFERROR(INDEX(Settings!${fc}${r1}:${lc}${r2},'
     'MATCH(%ABC class%,Settings!$A${r1}:$A${r2},0),'
     'MATCH(%Demand class%,Settings!${fc}${hr}:${lc}${hr},0)),{fallback}),'
     'IF(Inventory!{cc}{r}="yes",Settings!$B${s_clin},0)),{fallback})', PCT,
     "How often you intend to be able to supply, from the matrix on "
     "Settings. Clinically critical lines are floored at the clinical "
     "figure."),
    ("Safety factor",
     # The engine clamps the service level into [0.5, 0.9999] before
     # inverting, so a setting of 100% cannot produce an infinite z.
     '=NORMSINV(MIN(MAX(%Service target%,0.5),0.9999))', "0.00",
     "The z value for that service target."),
    ("Lead time, months",
     '=Inventory!{lt}{r}/{dpm}', NUM1,
     "The engine works in days and converts with a {dpm}-day month. The "
     "same divisor has to be used here or every reorder point drifts."),
    ("Lead time variability, months",
     '=Inventory!{ls}{r}/{dpm}', NUM1,
     "Lead-time standard deviation, on the same basis. Left blank on the "
     "Inventory sheet this is nil, which understates safety stock."),
    ("Safety stock",
     '=IF(%Months of history%<Settings!$B${s_minhist},"",'
     'MAX(0,%Safety factor%*SQRT(%Lead time, months%*'
     '%Demand variability%^2+%Mean monthly demand%^2*'
     '%Lead time variability, months%^2)))', NUM1,
     "z times the square root of demand variance over the lead time plus "
     "lead-time variance at the mean. Blank where there is no history to "
     "compute it from, which is itself the finding."),
    ("Reorder point",
     '=IF(%Safety stock%="","",'
     '%Mean monthly demand%*%Lead time, months%+%Safety stock%)',
     NUM1, "Order when the position reaches this."),
    ("Order up to",
     '=IF(%Safety stock%="","",'
     '%Reorder point%+Settings!$B$3*%Mean monthly demand%)', NUM1,
     "The reorder point plus the target cover on Settings."),
    ("Position",
     '=Inventory!{oh}{r}+Inventory!{po}{r}', NUM1,
     "On hand plus on order."),
    ("Months of cover",
     # On hand ONLY, not the position. Cover answers "how long can I keep
     # supplying from the shelf", and stock still on a ship cannot be
     # dispensed. The engine uses on_hand; using the position here read
     # every line with an open order as healthier than it is.
     '=IFERROR(Inventory!{oh}{r}/%Mean monthly demand%,"")', NUM1,
     "On-hand stock divided by monthly demand. Stock on order is "
     "deliberately not counted: it cannot be dispensed yet."),
    ("Days to expiry",
     '=IF(Inventory!{ex}{r}="","",Inventory!{ex}{r}-Settings!$B$2)', "0",
     "From the as-of date on Settings."),
    ("Expiry risk",
     # Stock that will not be consumed before it expires is a write-off,
     # whatever its cover figure says, so this is tested before anything
     # else except having no demand at all.
     '=IF(OR(%Days to expiry%="",%Mean monthly demand%<=0),"no",'
     'IF(%Days to expiry%<%Months of cover%*{dpm},"yes","no"))', "@",
     "Yes where the stock on hand will outlive its own expiry date."),
    ("State",
     # The engine's precedence, in the engine's order: no demand, then
     # expiry, then critical, then reorder, then excess. The order is the
     # whole point -- a line can satisfy several of these at once and the
     # first one that fits is the one worth acting on.
     '=IF(%Months of history%<Settings!$B${s_minhist},"no history",'
     'IF(%Mean monthly demand%<=0,IF(Inventory!{oh}{r}>0,"above policy",'
     '"within policy"),'
     'IF(%Expiry risk%="yes","expiry risk",'
     'IF(AND(%Position%<=%Reorder point%,%Months of cover%<1),"runs out",'
     'IF(%Position%<=%Reorder point%,"reorder",'
     'IF(%Months of cover%>Settings!$B$4,"above policy",'
     '"within policy"))))))', "@",
     "What this line is doing, in one word. Tested in the product's own "
     "order of precedence: expiry beats a short cover figure, and a short "
     "cover figure beats an excess one."),
    ("Suggested order",
     # Only when the state actually calls for one, and rounded UP: ordering
     # 589.4 units means ordering 590.
     '=IF(OR(%Order up to%="",'
     'AND(%State%<>"runs out",%State%<>"reorder")),0,'
     'MAX(0,CEILING(%Order up to%-%Position%,1)))', NUM1,
     "Nothing until the position falls to the reorder point; then enough "
     "to reach the order-up-to level, rounded up to whole units."),
    ("Order value",
     '=%Suggested order%*Inventory!{uc}{r}', MONEY, ""),
    ("Stock value",
     '=Inventory!{oh}{r}*Inventory!{uc}{r}', MONEY, ""),
    ("Above policy",
     # Two different questions, as in the engine. Excess stock is the
     # months of cover beyond the limit, priced at cost. Stock at expiry
     # risk is the units that will not be used before they expire. A line
     # in neither state contributes nothing, rather than a surplus
     # computed against a limit that was never breached.
     '=IF(%State%="above policy",'
     'MAX(0,%Months of cover%-Settings!$B$4)*%Mean monthly demand%'
     '*Inventory!{uc}{r},'
     'IF(%State%="expiry risk",'
     'MAX(0,Inventory!{oh}{r}-(%Mean monthly demand%/{dpm})'
     '*%Days to expiry%)*Inventory!{uc}{r},0))', MONEY,
     "Money standing still: cover beyond the excess limit, or stock that "
     "will expire before it is used."),
]


def write_stock_calc(ws, inventory_rows, svc):
    r = sheet_intro(ws, "Stock policy", [
        "The inventory calculation, written out. One row per line on the "
        "Inventory sheet, using that branch's own demand history from the "
        "Demand sheet.",
        "Safety stock is the standard formula: the service factor times the "
        "square root of demand variance over the lead time plus lead-time "
        "variance at the mean demand. Both halves matter, and leaving "
        "lead_time_days_std blank understates it.",
        "The product runs Croston and Holt forecasting on top of this, which "
        "a spreadsheet cannot do faithfully. These are the policy figures, "
        "not the forecast.",
    ])

    # Company as well as branch, because SUMIFS pairs its ranges by
    # POSITION, so a criteria range has to live on this sheet: the Inventory
    # sheet's data starts three rows higher and filtering against it silently
    # matched the wrong rows.
    heads = ["Item", "Branch", "Company"] + [h for h, *_ in STOCK_CALC]
    for j, h in enumerate(heads, start=1):
        c = ws.cell(row=r, column=j, value=h)
        c.font = HEADG if j > 3 else HEADW
        c.fill = PatternFill("solid", fgColor=GREY_HEAD if j > 3 else WHITE_HEAD)
        c.border = BOX
        c.alignment = Alignment(wrap_text=True, vertical="bottom")
        if j > 3:
            n = ws.cell(row=r - 1, column=j, value=STOCK_CALC[j - 4][3])
            n.font = NOTE
            n.alignment = Alignment(wrap_text=True, vertical="top")
    header = r
    ws.row_dimensions[r - 1].height = 58
    r += 1
    first_row = r

    letters = {h: get_column_letter(4 + i)
               for i, (h, *_rest) in enumerate(STOCK_CALC)}
    inv_spec = [c.name for c in samples.DATASETS["inventory"].columns]
    inv = {k: get_column_letter(inv_spec.index(v) + 1) for k, v in {
        "en": "entity_id", "lo": "location_id", "ik": "item_key",
        "oh": "on_hand", "po": "open_po", "uc": "unit_cost",
        "lt": "lead_time_days", "ls": "lead_time_days_std",
        "cc": "is_clinical_critical", "ex": "expiry_date",
    }.items()}
    # Everything the formulas need that is not a column letter: the matrix's
    # corners, the rows of the settings they read, and the engine's own
    # days-per-month constant, imported rather than retyped.
    cfg = {
        "fc": svc["first_class_col"], "lc": svc["last_class_col"],
        "hr": svc["class_row"], "r1": svc["first_abc_row"],
        "r2": svc["last_abc_row"],
        "s_clin": svc["at"]["clinical_critical_floor"],
        "s_adi": svc["at"]["adi_cutoff"],
        "s_cv2": svc["at"]["cv_squared_cutoff"],
        "s_minhist": svc["at"]["min_months_history"],
        "s_abca": svc["at"]["abc_a_cutoff"],
        "s_abcb": svc["at"]["abc_b_cutoff"],
        "dpm": ("%g" % DAYS_PER_MONTH),
        "fallback": SERVICE_FALLBACK,
    }

    for i in range(inventory_rows + 40):
        src = FIRST_DATA_ROW + i
        for j, (head, formula, fmt, _note) in enumerate(STOCK_CALC, start=4):
            body = formula
            # %%name%% is the column on its own, for a whole-column
            # reference; %name% is that column on THIS sheet's current row,
            # which is not the row the source data is on. Done longest first
            # so the double form is not eaten by the single one.
            for name, letter in letters.items():
                body = body.replace(f"%%{name}%%", letter)
            for name, letter in letters.items():
                body = body.replace(f"%{name}%", f"{letter}{r}")
            assert "%" not in body, head
            # r is the row on the SOURCE sheet; r2c is the row here. The
            # two differ because this sheet has an intro above its header.
            expr = body.format(r=src, r2c=r, **inv, **cfg)
            c = ws.cell(row=r, column=j,
                        value=f'=IF(Inventory!$C${src}="","",{expr[1:]})')
            style_grey(c)
            c.number_format = fmt
        for j, col in enumerate(("C", "B", "A"), start=1):
            c = ws.cell(row=r, column=j,
                        value=f'=IF(Inventory!$C${src}="","",'
                              f'Inventory!${col}${src})')
            style_grey(c)
            c.number_format = "@"
        r += 1
    last_row = r - 1

    ws.freeze_panes = ws.cell(row=header + 1, column=4)
    for j in range(1, len(heads) + 1):
        ws.column_dimensions[get_column_letter(j)].width = 14 if j > 3 else 16
    return {"header": header, "first": first_row, "last": last_row,
            "cols": letters}


PIPELINE_CALC = [
    # The product does not use the assumed odds once it has evidence. These
    # four columns are that calculation, written out, so the figure can be
    # argued with: how many deals have closed, how many of those were won,
    # what rate that implies, and which of the two the row actually used.
    ("Deals closed",
     '=COUNTIF(Pipeline!${st}:${st},"won")'
     '+COUNTIF(Pipeline!${st}:${st},"lost")', "0",
     "Won plus lost, across the whole sheet. Below the threshold on "
     "Settings the assumed odds are used instead of your own."),
    ("Deals won",
     '=COUNTIF(Pipeline!${st}:${st},"won")', "0", ""),
    ("Measured win rate",
     '=IFERROR(%Deals won%/%Deals closed%,"")', PCT,
     "Your own win rate, from what has actually closed."),
    ("Odds basis",
     '=IF(%Deals closed%>=Settings!$B${s_minclosed},"measured","assumed")',
     "@", "Which of the two the odds below came from. A weighted figure "
          "built on assumed odds is an opinion, not a measurement, and "
          "this column is what tells you which you are looking at."),
    ("Stage odds, unrounded",
     '=IF(OR(Pipeline!{st}{r}="won",Pipeline!{st}{r}="lost"),"",'
     'IFERROR(%Measured win rate%'
     '*IFERROR(LOOKUP(Pipeline!{st}{r},Lists!$M$2:$M$7,Lists!$N$2:$N$7),0)'
     '/LOOKUP("quoted",Lists!$M$2:$M$7,Lists!$N$2:$N$7),""))', "0.000000",
     "The measured rate scaled by how much likelier this stage is than "
     "'quoted', before rounding and before the cap. Shown so the next "
     "column is not a black box."),
    ("Odds at this stage",
     # Won and lost are facts, not odds. For an open stage: the measured
     # rate scaled by how much likelier this stage is than 'quoted', capped,
     # and rounded to four places exactly as the engine rounds it.
     '=IF(Pipeline!{st}{r}="won",1,IF(Pipeline!{st}{r}="lost",0,'
     'IF(%Odds basis%="measured",'
     # Rounded to four places the way the engine rounds, which is Python's
     # round(): half goes to the EVEN digit, not up. 0.78125 becomes 0.7812
     # here and 0.7813 under a plain ROUND. It is one part in ten thousand
     # of a probability, but a model that is meant to reproduce the platform
     # exactly should reproduce it exactly, and a tolerance wide enough to
     # swallow this is wide enough to hide a real error later.
     'MIN(Settings!$B${s_maxodds},'
     'IF(AND(ABS(%Stage odds, unrounded%*10000'
     '-TRUNC(%Stage odds, unrounded%*10000))=0.5,'
     'MOD(TRUNC(%Stage odds, unrounded%*10000),2)=0),'
     'TRUNC(%Stage odds, unrounded%*10000)/10000,'
     'ROUND(%Stage odds, unrounded%,4))),'
     'IFERROR(LOOKUP(Pipeline!{st}{r},Lists!$M$2:$M$7,Lists!$N$2:$N$7),0))))',
     PCT, "Measured where there is enough history, assumed where there is "
          "not. The Odds basis column says which."),
    ("Weighted value",
     '=Pipeline!{vl}{r}*%Odds at this stage%', MONEY,
     "Value times odds. This is not revenue and must never be added to it."),
    ("Days to close",
     '=IF(Pipeline!{ec}{r}="","",Pipeline!{ec}{r}-Settings!$B$2)', "0", ""),
    ("Held at this branch",
     '=IF(Pipeline!{ik}{r}="","",SUMIFS(Inventory!$F:$F,'
     'Inventory!$C:$C,Pipeline!{ik}{r},Inventory!$A:$A,Pipeline!{en}{r},'
     'Inventory!$B:$B,Pipeline!{lo}{r}))', NUM1, ""),
    ("Held by this company",
     '=IF(Pipeline!{ik}{r}="","",SUMIFS(Inventory!$F:$F,'
     'Inventory!$C:$C,Pipeline!{ik}{r},Inventory!$A:$A,Pipeline!{en}{r}))',
     NUM1, "Another company's stock is not counted: it would need an "
           "inter-company sale first."),
    ("Lead time, days",
     '=IF(Pipeline!{ik}{r}="","",IFERROR(_xlfn.MAXIFS(Inventory!$I:$I,'
     'Inventory!$C:$C,Pipeline!{ik}{r}),0))', "0", ""),
    ("Can we supply it",
     '=IF(Pipeline!{ik}{r}="","not linked to an item",'
     'IF(%Held by this company%="","item not in stock data",'
     'IF(AND(Pipeline!{qt}{r}>0,%Held at this branch%>=Pipeline!{qt}{r}),'
     '"in stock",'
     'IF(AND(Pipeline!{qt}{r}>0,%Held by this company%>=Pipeline!{qt}{r}),'
     '"in stock at another branch",'
     'IF(AND(%Days to close%<>"",%Lead time, days%>0,'
     '%Days to close%<%Lead time, days%),'
     '"cannot supply in time","needs ordering")))))', "@",
     "A tender closing inside the lead time of the item it needs is a "
     "disappointment with a date on it."),
]


def write_pipeline_calc(ws, pipeline_rows, svc):
    r = sheet_intro(ws, "Pipeline calculation", [
        "Each open deal weighted, and checked against the stock on the "
        "Inventory sheet.",
        "Stock is counted at the deal's own branch first, then across its "
        "own company. Another legal entity's stock is not available without "
        "an inter-company sale, so it is not counted at all.",
    ])
    heads = ["Opportunity", "Customer", "Stage"] + [h for h, *_ in PIPELINE_CALC]
    for j, h in enumerate(heads, start=1):
        c = ws.cell(row=r, column=j, value=h)
        c.font = HEADG if j > 3 else HEADW
        c.fill = PatternFill("solid", fgColor=GREY_HEAD if j > 3 else WHITE_HEAD)
        c.border = BOX
        c.alignment = Alignment(wrap_text=True, vertical="bottom")
        if j > 3:
            n = ws.cell(row=r - 1, column=j, value=PIPELINE_CALC[j - 4][3])
            n.font = NOTE
            n.alignment = Alignment(wrap_text=True, vertical="top")
    header = r
    ws.row_dimensions[r - 1].height = 52
    r += 1
    first_row = r

    letters = {h: get_column_letter(4 + i)
               for i, (h, *_rest) in enumerate(PIPELINE_CALC)}
    spec = [c.name for c in samples.DATASETS["opportunities"].columns]
    op = {k: get_column_letter(spec.index(v) + 1) for k, v in {
        "en": "entity_id", "lo": "location_id", "st": "stage",
        "vl": "value", "ec": "expected_close", "ik": "item_key",
        "qt": "quantity",
    }.items()}

    for i in range(pipeline_rows + 40):
        src = FIRST_DATA_ROW + i
        for j, (head, formula, fmt, _note) in enumerate(PIPELINE_CALC, start=4):
            body = formula
            for name, letter in letters.items():
                body = body.replace(f"%%{name}%%", letter)
            for name, letter in letters.items():
                body = body.replace(f"%{name}%", f"{letter}{r}")
            assert "%" not in body, head
            expr = body.format(
                r=src, **op,
                s_minclosed=svc["at"]["min_closed_for_measured_odds"],
                s_maxodds=svc["at"]["max_stage_odds"],
            )
            c = ws.cell(row=r, column=j,
                        value=f'=IF(Pipeline!$A${src}="","",{expr[1:]})')
            style_grey(c)
            c.number_format = fmt
        for j, col in enumerate(("A", "E", "F"), start=1):
            c = ws.cell(row=r, column=j,
                        value=f'=IF(Pipeline!$A${src}="","",'
                              f'Pipeline!${col}${src})')
            style_grey(c)
            c.number_format = "@"
        r += 1
    last_row = r - 1

    t = r + 1
    ws.cell(row=t, column=3, value="Totals").font = H2
    for j, (head, _f, fmt, _n) in enumerate(PIPELINE_CALC, start=4):
        if fmt != MONEY:
            continue
        col = get_column_letter(j)
        c = ws.cell(row=t, column=j,
                    value=f"=SUM({col}{first_row}:{col}{last_row})")
        c.font = Font(name=FONT, size=10, bold=True, color=INK)
        c.number_format = MONEY
        c.border = Border(top=thin)

    ws.freeze_panes = ws.cell(row=header + 1, column=4)
    for j in range(1, len(heads) + 1):
        ws.column_dimensions[get_column_letter(j)].width = 16 if j > 3 else 20
    return {"header": header, "first": first_row, "last": last_row,
            "cols": letters, "totals": t}


def write_summary(ws, pnl, bs, cf, stock, payroll, pipeline):
    r = sheet_intro(ws, "Summary", [
        "Everything the workbook works out, on one page. Every figure is a "
        "formula off the sheets behind it, so this moves the moment an input "
        "does.",
    ])
    P, B, C = "'Profit and loss'", "'Balance sheet'", "'Cash flow'"
    SP, PC, PL = "'Stock policy'", "'Payroll calculation'", "'Pipeline calculation'"
    sp_first, sp_last = stock["first"], stock["last"]
    sc = stock["cols"]

    ws.cell(row=r, column=1, value="THE YEAR").font = HEADG
    r += 1
    r = report_row(ws, r, "Revenue", f"={P}!$B${pnl['revenue']}")
    r = report_row(ws, r, "Gross profit", f"={P}!$B${pnl['gross']}")
    r = report_row(ws, r, "Profit after tax", f"={P}!$B${pnl['pat']}", bold=True)
    r = report_row(ws, r, "Cash from operations", f"={C}!$B${cf['cfo']}",
                   note="Compare with profit after tax: the gap is working "
                        "capital.")
    r = report_row(ws, r, "Net movement in cash", f"={C}!$B${cf['net']}")
    r += 1

    ws.cell(row=r, column=1, value="WHAT IT OWNS AND OWES").font = HEADG
    r += 1
    r = report_row(ws, r, "Total assets", f"={B}!$C${bs['assets']}")
    r = report_row(ws, r, "Total liabilities", f"={B}!$C${bs['liabilities']}")
    r = report_row(ws, r, "Equity", f"={B}!$C${bs['equity']}", bold=True)
    r = report_row(ws, r, "Cash at the end", f"={B}!$C${bs['cash']}")
    r += 1

    ws.cell(row=r, column=1, value="STOCK").font = HEADG
    r += 1
    r = report_row(ws, r, "Stock value",
                   f"=SUM({SP}!{sc['Stock value']}{sp_first}:"
                   f"{sc['Stock value']}{sp_last})")
    r = report_row(ws, r, "Held above policy",
                   f"=SUM({SP}!{sc['Above policy']}{sp_first}:"
                   f"{sc['Above policy']}{sp_last})",
                   note="Financed at the cost of capital on Settings while "
                        "it sits there.")
    r = report_row(ws, r, "Cost of carrying that",
                   f"=B{r-1}*Settings!$B$5",
                   note="A year, at the cost of capital.")
    r = report_row(ws, r, "Suggested orders",
                   f"=SUM({SP}!{sc['Order value']}{sp_first}:"
                   f"{sc['Order value']}{sp_last})")
    r = report_row(ws, r, "Lines that run out",
                   f'=COUNTIF({SP}!{sc["State"]}{sp_first}:'
                   f'{sc["State"]}{sp_last},"runs out")', fmt="0",
                   note="Under a month of cover.")
    r = report_row(ws, r, "Lines with no history",
                   f'=COUNTIF({SP}!{sc["State"]}{sp_first}:'
                   f'{sc["State"]}{sp_last},"no history")', fmt="0",
                   note="Policy for these rests on nothing. Export more "
                        "demand history.")
    r += 1

    ws.cell(row=r, column=1, value="RECEIVABLES").font = HEADG
    r += 1
    rec = "Receivables"
    ar_open = get_column_letter(GREY_START)          # calc_open_amount
    ar_bucket = get_column_letter(GREY_START + 2)    # calc_bucket
    r = report_row(ws, r, "Owed",
                   f"=SUM('{rec}'!{ar_open}$4:{ar_open}$1000)")
    for label, bucket in (("Not yet due", "current"), ("91 to 180", "91-180"),
                          ("Over 180", "180+")):
        r = report_row(ws, r, label,
                       f"=SUMIF('{rec}'!{ar_bucket}$4:{ar_bucket}$1000,"
                       f'"{bucket}",'
                       f"'{rec}'!{ar_open}$4:{ar_open}$1000)", indent=True)
    r = report_row(ws, r, "Cost of carrying the overdue book",
                   f"=(B{r-4}-B{r-3})*Settings!$B$5",
                   note="Everything past due, financed for a year.")
    r += 1

    ws.cell(row=r, column=1, value="PEOPLE").font = HEADG
    r += 1
    pc = payroll["cols"]
    r = report_row(ws, r, "On the payroll",
                   f'=COUNTIF({PC}!$A${payroll["first"]}:'
                   f'$A${payroll["last"]},"?*")', fmt="0")
    r = report_row(ws, r, "Gross a month",
                   f"={PC}!{pc['Gross a month']}{payroll['totals']}")
    r = report_row(ws, r, "Cost to employer, a month",
                   f"={PC}!{pc['Cost to employer']}{payroll['totals']}",
                   bold=True)
    r = report_row(ws, r, "Cost to employer, a year",
                   f"=B{r-1}*12")
    r = report_row(ws, r, "On top of gross",
                   f"=IFERROR(B{r-2}/B{r-3}-1,0)", fmt=PCT,
                   note="Contributions, provident fund and accrued gratuity.")
    r = report_row(ws, r, "Tax withheld a month",
                   f"={PC}!{pc['Tax a month']}{payroll['totals']}")
    r += 1

    ws.cell(row=r, column=1, value="PIPELINE").font = HEADG
    r += 1
    plc = pipeline["cols"]
    r = report_row(ws, r, "Weighted pipeline",
                   f"={PL}!{plc['Weighted value']}{pipeline['totals']}",
                   note="Not revenue. Never add it to revenue.")
    r = report_row(ws, r, "Cannot be supplied in time",
                   f'=COUNTIF({PL}!{plc["Can we supply it"]}'
                   f'${pipeline["first"]}:{plc["Can we supply it"]}'
                   f'${pipeline["last"]},"cannot supply in time")', fmt="0",
                   note="Closing inside the lead time of the item it needs.")
    r = report_row(ws, r, "Marketing spend",
                   "=SUM('Marketing spend'!$E$4:$E$200)")
    r += 2

    ws.cell(row=r, column=1, value="WHAT THIS WORKBOOK DOES NOT DO").font = HEADG
    r += 1
    for line in (
        "Forecasting. Croston and Holt need iteration a spreadsheet cannot "
        "carry honestly, so there is no forecast here. The policy figures on "
        "Stock policy do not depend on one.",
        "The thirteen-week cash view. It times each customer's receipts from "
        "their own payment behaviour, week by week, which is a simulation "
        "rather than a formula.",
        "Transfer suggestions and order sequencing. Both allocate across "
        "branches and against a cash limit, and both need to decide in an "
        "order rather than all at once.",
        "Stage win rates measured from your own closed deals. The odds on "
        "Lists are assumptions; the product measures them once enough has "
        "closed.",
        "Upload the workbook and the product does all four.",
    ):
        c = ws.cell(row=r, column=1, value="•  " + line)
        c.font = NOTE
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        ws.row_dimensions[r].height = 15 * (1 + len(line) // 95)
        r += 1

    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 62


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sop-insight-data-model.xlsx")
