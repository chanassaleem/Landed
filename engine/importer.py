"""Reading a customer's workbook, and writing it into their organisation.

Two stages, and the split is the point. `plan` reads the file, checks
it, and reports what it would do; `apply` does it. Nobody should
discover what an import did by looking at the result, least of all on
their own live data, and a one-stage importer gives them no other way
to find out.

Everything `apply` writes goes in one transaction. A half-finished
import is worse than a refused one: the second attempt then runs into
rows the first one already made, and the person is left reconciling two
partial states by hand.

Note what is not here: no org_id anywhere. The connection comes from
`as_user` and the policies decide which organisation these rows belong
to. The columns are filled by the defaults in the schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

REQUIRED_SHEETS = ("Branches", "Items", "Stock")
OPTIONAL_SHEETS = ("Sales history", "On order", "Shipments", "Suppliers",
                   "Customers", "Invoices", "Machines", "Service jobs")

# The model, said once.
#
# What each sheet is, where it lands, and what it switches on. It lives
# beside the readers because that is where the truth is: a list kept
# anywhere else goes stale the first time a sheet is added, which is
# exactly what happened to the template this application used to serve
# -- it shared six sheet names out of twenty-four and was refused by
# this importer.
#
# `tables` is where the rows end up. A person filling in a spreadsheet
# does not need to know that, but somebody deciding whether to trust
# the software does, and nobody else is going to tell them.
SHEET_MODEL: tuple[dict, ...] = (
    dict(sheet="Branches", tables=("locations",),
         what="Every branch you hold stock at.",
         unlocks="Everything."),
    dict(sheet="Items", tables=("items", "suppliers"),
         what="Every line you carry, once \u2014 not once per branch.",
         unlocks="The catalogue the rest reads."),
    dict(sheet="Stock", tables=("stock_movement", "item_locations"),
         what="What is on the shelf today. Your count BEFORE anything "
              "on Goods received.",
         unlocks="Stock value, and what stands still."),
    dict(sheet="Sales history", tables=("demand_history",),
         what="What you sold each month. Fills the months before you "
              "start recording sales here.",
         unlocks="Forecast, cover, reorder points."),
    dict(sheet="On order", tables=("purchase_orders", "po_lines"),
         what="Orders placed, in the supplier's currency with the rate "
              "you got.",
         unlocks="What is already coming."),
    dict(sheet="Shipments", tables=("shipments", "shipment_costs"),
         what="What is in transit, and the freight and duty on it.",
         unlocks="Landing, with Goods received."),
    dict(sheet="Goods received", tables=("goods_receipts", "grn_lines",
                                         "shipment_lines"),
         what="What actually arrived off those shipments.",
         unlocks="The landed unit cost."),
    dict(sheet="Fitment", tables=("item_machines",),
         what="Which analyser each reagent runs on.",
         unlocks="Dead stock, or a machine consumable."),
    dict(sheet="Suppliers", tables=("suppliers",),
         what="Who you buy from and in what currency.",
         unlocks="Optional: taken from Items."),
    dict(sheet="Customers", tables=("customers",),
         what="Who you sell to, their terms and credit limit.",
         unlocks="Owed, and who is over their limit."),
    dict(sheet="Invoices", tables=("invoices",),
         what="What you billed and what has been paid.",
         unlocks="Owed, Sales, cash coming in."),
    dict(sheet="Contacts", tables=("contacts",),
         what="People at those customers.",
         unlocks="Customers."),
    dict(sheet="Dealings", tables=("interactions",),
         what="What was said and when.",
         unlocks="Customers."),
    dict(sheet="Machines", tables=("machines",),
         what="Analysers placed with customers.",
         unlocks="Equipment, and whether it repays."),
    dict(sheet="Service jobs", tables=("service_jobs",),
         what="Visits, hours, parts, downtime.",
         unlocks="Service, and the margin lost."),
    dict(sheet="Pipeline", tables=("opportunities",),
         what="Tenders and accounts being worked.",
         unlocks="Pipeline, against stock and cash."),
    dict(sheet="Marketing spend", tables=("marketing_spend",),
         what="What was spent winning work.",
         unlocks="Cost per win."),
    dict(sheet="Trial balance", tables=("accounts",),
         what="Your trial balance for a period.",
         unlocks="Books, and the checks that tie."),
    dict(sheet="Dividends", tables=("equity_movements",),
         what="Dividends and capital in or out.",
         unlocks="The retained-earnings check."),
    dict(sheet="Committed payments", tables=("commitments",),
         what="Payments already committed, by week.",
         unlocks="Cash, and what Ordering refuses."),
    dict(sheet="Bank balance", tables=("cash_balances",),
         what="What is in the bank, as at a date.",
         unlocks="Cash. The thirteen weeks need it."),
    dict(sheet="Employees", tables=("employees",),
         what="Who you employ and on what.",
         unlocks="Payroll, and the floor under cash."),
    dict(sheet="Read me", tables=(),
         what="What each sheet is for.",
         unlocks="Not read by the importer."),
    dict(sheet="Check", tables=(),
         what="Totals that must tie before you upload.",
         unlocks="Not read by the importer."),
)

PERIOD = re.compile(r"^\d{4}-\d{2}$")
MONTH_COL = re.compile(r"^sold[_ ](\d{4})[_-](\d{2})$", re.I)

TRUE = {"yes", "y", "true", "1", "t"}


def _yes(v) -> bool:
    """A tick box, however somebody wrote it.

    The existing readers spell this out inline; there are ten of them
    now, and ten copies of the same expression is ten chances for one
    of them to read "y" differently from the rest.
    """
    return _text(v).lower() in TRUE


class NoOrganisation(Exception):
    """The caller has no organisation, so there is nowhere to write."""


class BadWorkbook(Exception):
    """The file cannot be read at all. Distinct from a problem inside a
    sheet, which is reported as a Problem and does not stop the read."""


@dataclass
class Problem:
    sheet: str
    row: int | None
    message: str
    fatal: bool = True


@dataclass
class Plan:
    branches: list[dict] = field(default_factory=list)
    suppliers: list[dict] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    stock: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    shipments: list[dict] = field(default_factory=list)
    customers: list[dict] = field(default_factory=list)
    invoices: list[dict] = field(default_factory=list)
    machines: list[dict] = field(default_factory=list)
    jobs: list[dict] = field(default_factory=list)
    # Migration 0009's tables. Each one switches on the part of the
    # app that reads it, so a workbook with four sheets filled in is
    # not a broken import -- it is stock and reordering, and the rest
    # when they have it.
    accounts: list[dict] = field(default_factory=list)
    dividends: list[dict] = field(default_factory=list)
    commitments: list[dict] = field(default_factory=list)
    bank: list[dict] = field(default_factory=list)
    employees: list[dict] = field(default_factory=list)
    pipeline: list[dict] = field(default_factory=list)
    spend: list[dict] = field(default_factory=list)
    contacts: list[dict] = field(default_factory=list)
    dealings: list[dict] = field(default_factory=list)
    fitment: list[dict] = field(default_factory=list)
    receipts: list[dict] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    months: list[str] = field(default_factory=list)

    @property
    def fatal(self) -> list[Problem]:
        return [p for p in self.problems if p.fatal]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if not p.fatal]

    @property
    def ok(self) -> bool:
        return not self.fatal and bool(self.items)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "counts": {
                "branches": len(self.branches),
                "suppliers": len(self.suppliers),
                "items": len(self.items),
                "stock": len(self.stock),
                "history": len(self.history),
                "orders": len(self.orders),
                "shipment_costs": len(self.shipments),
                "receipts": len(self.receipts),
                "fitment": len(self.fitment),
                "customers": len(self.customers),
                "invoices": len(self.invoices),
                "machines": len(self.machines),
                "service_jobs": len(self.jobs),
                "accounts": len(self.accounts),
                "dividends": len(self.dividends),
                "commitments": len(self.commitments),
                "bank_balances": len(self.bank),
                "employees": len(self.employees),
                "pipeline": len(self.pipeline),
                "marketing_spend": len(self.spend),
                "contacts": len(self.contacts),
                "dealings": len(self.dealings),
            },
            # What the preview shows, in order, named here rather than
            # in the browser. The browser kept its own list of seven
            # labels against the eighteen counts above, so a person
            # uploading employees, invoices, machines or a pipeline
            # saw none of them confirmed -- and the two sheets added
            # most recently were invisible for the same reason. A list
            # the server owns cannot drift from the counts beside it.
            "summary": [
                {"label": label, "rows": n}
                for label, n in (
                    ("Branches", len(self.branches)),
                    ("Suppliers", len(self.suppliers)),
                    ("Items", len(self.items)),
                    ("Stock lines", len(self.stock)),
                    ("Months of history", len(self.history)),
                    ("Open orders", len(self.orders)),
                    ("Shipment costs", len(self.shipments)),
                    ("Goods received", len(self.receipts)),
                    ("Fitment", len(self.fitment)),
                    ("Customers", len(self.customers)),
                    ("Invoices", len(self.invoices)),
                    ("Placed equipment", len(self.machines)),
                    ("Service jobs", len(self.jobs)),
                    ("Trial balance", len(self.accounts)),
                    ("Dividends", len(self.dividends)),
                    ("Committed payments", len(self.commitments)),
                    ("Bank balances", len(self.bank)),
                    ("Employees", len(self.employees)),
                    ("Pipeline", len(self.pipeline)),
                    ("Marketing spend", len(self.spend)),
                    ("Contacts", len(self.contacts)),
                    ("Dealings", len(self.dealings)),
                )
                if n
            ],
            "months": [self.months[0], self.months[-1]] if self.months else [],
            "value": round(sum(s["qty"] * s["cost"] for s in self.stock), 2),
            "problems": [
                {"sheet": p.sheet, "row": p.row, "message": p.message,
                 "fatal": p.fatal}
                for p in self.problems[:60]
            ],
            "problem_count": len(self.problems),
        }


# ------------------------------------------------------------- reading

def _text(v) -> str:
    """Whatever the cell holds, as a trimmed string.

    Excel returns more than str and None: a formula cell can come back
    as a float, a date cell as a datetime, and a cell with mixed
    formatting as rich text. str() handles all of them; the point is
    that nothing downstream has to.
    """
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        # 4417.0 is a SKU somebody typed as a number. Matching it
        # against "4417" on another sheet has to work.
        return str(int(v))
    return str(v).strip()


def _num(v, default=0.0) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _three(code: str, fallback: str = "PKR") -> str:
    """A currency the column constraint will accept.

    The schema requires exactly three characters. A blank cell, or
    "Rs", reaches the database as a value it refuses -- and the refusal
    arrives at the end of a long transaction, after everything else has
    already been written and is about to be rolled back.
    """
    code = (code or "").strip().upper()
    return code if len(code) == 3 and code.isalpha() else fallback


def _is_num(v) -> bool:
    if v is None or v == "":
        return True            # blank is allowed; it means the default
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _day(v):
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _rows(ws, headers: list[str]):
    """Rows as dicts, stopping at the first blank first column.

    The sheets carry footnotes under the data. Reading to max_row would
    hand the importer a row whose SKU is a sentence of advice -- which
    is exactly what happened the first time.
    """
    index = {h: i for i, h in enumerate(headers)}
    for r in range(2, ws.max_row + 1):
        first = ws.cell(r, 1).value
        if first is None or _text(first) == "":
            break
        yield r, [ws.cell(r, c + 1).value for c in range(len(headers))], index


def _headers(ws) -> list[str]:
    out = []
    for c in range(1, ws.max_column + 1):
        h = _text(ws.cell(1, c).value)
        out.append(h.rstrip(" *").strip())
    return out


def plan(data: bytes) -> Plan:
    """Read and check, without touching the database."""
    from io import BytesIO

    from openpyxl import load_workbook

    try:
        wb = load_workbook(BytesIO(data), data_only=True, read_only=False)
    except Exception as exc:
        raise BadWorkbook("That file could not be opened as a workbook.") from exc

    p = Plan()
    missing = [s for s in REQUIRED_SHEETS if s not in wb.sheetnames]
    if missing:
        p.problems.append(Problem(
            "workbook", None,
            f"These sheets are missing: {', '.join(missing)}."))
        return p

    _read_branches(wb["Branches"], p)
    if "Suppliers" in wb.sheetnames:
        _read_suppliers(wb["Suppliers"], p)
    _read_items(wb["Items"], p)
    _read_stock(wb["Stock"], p)
    if "Sales history" in wb.sheetnames:
        _read_history(wb["Sales history"], p)
    if "On order" in wb.sheetnames:
        _read_orders(wb["On order"], p)
    if "Shipments" in wb.sheetnames:
        _read_shipments(wb["Shipments"], p)
    if "Customers" in wb.sheetnames:
        _read_customers(wb["Customers"], p)
    if "Invoices" in wb.sheetnames:
        _read_invoices(wb["Invoices"], p)
    if "Machines" in wb.sheetnames:
        _read_machines(wb["Machines"], p)
    if "Service jobs" in wb.sheetnames:
        _read_jobs(wb["Service jobs"], p)
    if "Goods received" in wb.sheetnames:
        _read_receipts(wb["Goods received"], p)
    if "Fitment" in wb.sheetnames:
        _read_fitment(wb["Fitment"], p)

    # Every one of these is optional, and a workbook written before
    # they existed is still a valid import. That is why each is guarded
    # rather than listed in REQUIRED_SHEETS: somebody who filled in a
    # workbook last month should not be told their file is wrong.
    for sheet, reader in (
        ("Trial balance", _read_accounts),
        ("Dividends", _read_dividends),
        ("Committed payments", _read_commitments),
        ("Bank balance", _read_bank),
        ("Employees", _read_employees),
        ("Pipeline", _read_pipeline),
        ("Marketing spend", _read_spend),
        ("Contacts", _read_contacts),
        ("Dealings", _read_dealings),
    ):
        if sheet in wb.sheetnames:
            reader(wb[sheet], p)

    _cross_check(p)
    return p


def _read_branches(ws, p):
    h = _headers(ws)
    seen = set()
    for r, vals, i in _rows(ws, h):
        code = _text(vals[i.get("branch_code", 0)]).upper()
        name = _text(vals[i["branch_name"]]) if "branch_name" in i else ""
        if not code:
            p.problems.append(Problem("Branches", r, "No branch code."))
            continue
        if code in seen:
            p.problems.append(Problem("Branches", r,
                                      f"{code} appears more than once."))
            continue
        seen.add(code)
        p.branches.append({
            "code": code,
            "name": name or code,
            "city": _text(vals[i["city"]]) if "city" in i else "",
            "warehouse": _text(vals[i.get("is_warehouse", -1)]
                               if "is_warehouse" in i else "").lower() in TRUE,
        })


def _read_suppliers(ws, p):
    h = _headers(ws)
    for r, vals, i in _rows(ws, h):
        name = _text(vals[i.get("supplier_name", 0)])
        if name:
            p.suppliers.append({
                "name": name,
                "country": _text(vals[i["country"]]) if "country" in i else "",
                "currency": _three(_text(vals[i["currency"]])
                                   if "currency" in i else ""),
            })


def _read_items(ws, p):
    h = _headers(ws)
    seen = set()
    for r, vals, i in _rows(ws, h):
        sku = _text(vals[i.get("sku", 0)])
        if not sku:
            p.problems.append(Problem("Items", r, "No SKU."))
            continue
        if sku in seen:
            p.problems.append(Problem(
                "Items", r,
                f"{sku} appears more than once. Each SKU goes in once, or "
                f"the importer has to guess which row is true."))
            continue
        seen.add(sku)
        if "lead_time_days" in i and not _is_num(vals[i["lead_time_days"]]):
            p.problems.append(Problem(
                "Items", r, f"{sku}: lead_time_days is not a number."))
            continue
        p.items.append({
            "sku": sku,
            "description": (_text(vals[i["description"]])
                            if "description" in i else "") or sku,
            "category": _text(vals[i["category"]]) if "category" in i else "",
            "supplier": _text(vals[i["supplier_name"]]) if "supplier_name" in i else "",
            "uom": (_text(vals[i["uom"]]) if "uom" in i else "") or "each",
            "lead": _num(vals[i["lead_time_days"]]) if "lead_time_days" in i else 0,
            "lead_std": _num(vals[i["lead_time_days_std"]]) if "lead_time_days_std" in i else 0,
            "critical": (_text(vals[i["is_clinical_critical"]]).lower() in TRUE
                         if "is_clinical_critical" in i else False),
            # None rather than zero for a missing price. Zero is a
            # price -- it would make the margin on the line the whole
            # negative landed cost -- and a blank cell means nobody
            # has said yet.
            "price": (_num(vals[i["list_price"]])
                      if "list_price" in i
                      and _is_num(vals[i["list_price"]]) else None),
            "generic": (_text(vals[i["is_generic"]]).lower() in TRUE
                        if "is_generic" in i else False),
        })


def _read_stock(ws, p):
    h = _headers(ws)
    seen = set()
    for r, vals, i in _rows(ws, h):
        sku = _text(vals[i.get("sku", 0)])
        branch = _text(vals[i["branch_code"]]).upper() if "branch_code" in i else ""
        if not sku or not branch:
            p.problems.append(Problem("Stock", r, "Needs both a SKU and a branch."))
            continue
        key = (sku, branch)
        if key in seen:
            p.problems.append(Problem(
                "Stock", r, f"{sku} at {branch} appears twice."))
            continue
        seen.add(key)
        qty = _num(vals[i["qty_on_hand"]]) if "qty_on_hand" in i else 0
        if qty < 0:
            p.problems.append(Problem(
                "Stock", r, f"{sku} at {branch}: a count cannot be negative."))
            continue
        p.stock.append({
            "sku": sku, "branch": branch, "qty": qty,
            "cost": _num(vals[i["unit_cost"]]) if "unit_cost" in i else 0,
            "expiry": _day(vals[i["expiry_date"]]) if "expiry_date" in i else None,
        })


def _read_history(ws, p):
    h = _headers(ws)
    months = []
    for c, head in enumerate(h):
        m = MONTH_COL.match(head)
        if m:
            months.append((c, f"{m.group(1)}-{m.group(2)}"))
    if not months:
        p.problems.append(Problem(
            "Sales history", None,
            "No month columns found. They are named sold_YYYY_MM.",
            fatal=False))
        return
    p.months = [m for _c, m in months]

    i = {name: n for n, name in enumerate(h)}
    for r, vals, _i in _rows(ws, h):
        sku = _text(vals[i.get("sku", 0)])
        branch = _text(vals[i["branch_code"]]).upper() if "branch_code" in i else ""
        if not sku or not branch:
            continue
        for c, period in months:
            v = vals[c] if c < len(vals) else None
            if v is None or v == "":
                # Blank means none sold that month, which is a fact the
                # forecast uses. A month nobody has a record for is left
                # out of the sheet entirely -- see the Read me.
                q = 0.0
            elif not _is_num(v):
                p.problems.append(Problem(
                    "Sales history", r,
                    f"{sku} at {branch}, {period}: not a number.", fatal=False))
                continue
            else:
                q = max(0.0, _num(v))
            p.history.append({"sku": sku, "branch": branch,
                              "period": period, "qty": q})


def _read_orders(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    for r, vals, _i in _rows(ws, h):
        po = _text(vals[i.get("po_number", 0)])
        sku = _text(vals[i["sku"]]) if "sku" in i else ""
        branch = _text(vals[i["branch_code"]]).upper() if "branch_code" in i else ""
        if not (po and sku and branch):
            p.problems.append(Problem(
                "On order", r, "Needs a PO number, a SKU and a branch.",
                fatal=False))
            continue
        p.orders.append({
            "po": po, "sku": sku, "branch": branch,
            "supplier": _text(vals[i["supplier_name"]]) if "supplier_name" in i else "",
            "qty": _num(vals[i["qty"]]) if "qty" in i else 0,
            "price": _num(vals[i["unit_price"]]) if "unit_price" in i else 0,
            "currency": _three(_text(vals[i["currency"]])
                               if "currency" in i else ""),
            "fx": _num(vals[i["fx_rate"]], 1.0) or 1.0,
            "ordered": _day(vals[i["ordered_at"]]) if "ordered_at" in i else None,
            "expected": _day(vals[i["expected_at"]]) if "expected_at" in i else None,
            "status": (_text(vals[i["status"]]) if "status" in i else "") or "placed",
        })


COST_KINDS = ("freight", "duty", "clearing", "inland", "insurance",
              "bank", "other")

# What people actually write in that column, mapped to the kind the
# table accepts. The typed words are kept as the note, so mapping
# "Ocean freight" to `freight` never loses which freight it was.
#
# Matched on whole words, not substrings. "Inland transport" contains
# "port", so a substring match filed every inland charge as clearing
# -- and because both are real kinds, nothing anywhere said so: the
# total was right and the breakdown was wrong.
COST_WORDS = (
    ("freight", ("freight", "ocean", "sea", "airfreight", "waybill")),
    ("duty", ("duty", "duties", "customs", "tariff", "levy")),
    ("inland", ("inland", "transport", "transportation", "trucking",
                "haulage", "cartage", "delivery")),
    ("clearing", ("clearing", "clearance", "agent", "port", "handling",
                  "demurrage", "terminal", "wharfage")),
    ("insurance", ("insurance", "marine")),
    ("bank", ("bank", "lc", "swift", "credit")),
)

ALLOCATE_BY = ("value", "weight", "volume", "qty")

_WORD = re.compile(r"[a-z]+")


def _cost_kind(text: str) -> str:
    words = set(_WORD.findall((text or "").lower()))
    for kind, names in COST_WORDS:
        if words & set(names):
            return kind
    return "other"


def _read_shipments(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    for r, vals, _i in _rows(ws, h):
        ref = _text(vals[i.get("shipment_ref", 0)])
        if not ref:
            continue
        typed = _text(vals[i["cost_type"]]) if "cost_type" in i else ""
        basis = (_text(vals[i["allocate_by"]]).lower()
                 if "allocate_by" in i else "value") or "value"
        if basis not in ALLOCATE_BY:
            p.problems.append(Problem(
                "Shipments", r,
                f"{basis} is not a way to spread a cost. One of: "
                f"{', '.join(ALLOCATE_BY)}.", fatal=False))
            continue
        p.shipments.append({
            "ref": ref,
            "po": _text(vals[i["po_number"]]) if "po_number" in i else "",
            "mode": _text(vals[i["mode"]]).lower() if "mode" in i else "",
            "etd": _day(vals[i["etd"]]) if "etd" in i else None,
            "eta": _day(vals[i["eta"]]) if "eta" in i else None,
            "cost_type": typed,
            "kind": _cost_kind(typed),
            # `amount` is the column now; `amount_pkr` is what the
            # sheet was called before a cost could be in anything but
            # rupees, and a workbook filled in last month is still a
            # valid workbook.
            "amount": (_num(vals[i["amount"]]) if "amount" in i
                       else _num(vals[i["amount_pkr"]])
                       if "amount_pkr" in i else 0),
            # A shipping line that bills in dollars was being recorded
            # as dollars-worth of rupees. The allocation view has
            # multiplied by fx_rate since it was written; nothing was
            # setting it, so freight on a USD invoice landed at a
            # 282nd of its real weight.
            "currency": ((_text(vals[i["currency"]]).upper()
                          if "currency" in i else "") or "PKR"),
            "fx": (_num(vals[i["fx_rate"]])
                   if "fx_rate" in i and _is_num(vals[i["fx_rate"]])
                   else None),
            "allocate_by": basis,
        })


def _read_customers(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    seen = set()
    for r, vals, _i in _rows(ws, h):
        code = _text(vals[i.get("customer_code", 0)])
        name = _text(vals[i["customer_name"]]) if "customer_name" in i else ""
        if not code or not name:
            p.problems.append(Problem("Customers", r,
                                      "Needs a code and a name."))
            continue
        if code in seen:
            p.problems.append(Problem("Customers", r,
                                      f"{code} appears more than once."))
            continue
        seen.add(code)
        p.customers.append({
            "code": code, "name": name,
            "city": _text(vals[i["city"]]) if "city" in i else "",
            "public": (_text(vals[i["is_public_sector"]]).lower() in TRUE
                       if "is_public_sector" in i else False),
            "inter": (_text(vals[i["is_intercompany"]]).lower() in TRUE
                      if "is_intercompany" in i else False),
            "terms": int(_num(vals[i["payment_terms_days"]], 30))
                     if "payment_terms_days" in i else 30,
            "contact": _text(vals[i["contact_name"]]) if "contact_name" in i else "",
            "email": _text(vals[i["contact_email"]]) if "contact_email" in i else "",
        })


def _read_invoices(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    for r, vals, _i in _rows(ws, h):
        no = _text(vals[i.get("invoice_no", 0)])
        cust = _text(vals[i["customer_code"]]) if "customer_code" in i else ""
        issued = _day(vals[i["invoice_date"]]) if "invoice_date" in i else None
        if not (no and cust and issued):
            p.problems.append(Problem(
                "Invoices", r, "Needs a number, a customer and a date."))
            continue
        amount = _num(vals[i["amount"]]) if "amount" in i else 0
        paid = _num(vals[i["amount_paid"]]) if "amount_paid" in i else 0
        if paid > amount:
            # Not clamped: more paid than invoiced is a real mistake
            # somewhere, and silently flooring it hides which.
            p.problems.append(Problem(
                "Invoices", r,
                f"{no}: paid ({paid:,.0f}) is more than the invoice "
                f"({amount:,.0f})."))
            continue
        p.invoices.append({
            "no": no, "customer": cust, "date": issued,
            "due": _day(vals[i["due_date"]]) if "due_date" in i else issued,
            "branch": _text(vals[i["branch_code"]]).upper()
                      if "branch_code" in i else "",
            "amount": amount, "paid": paid,
        })


def _read_receipts(ws, p):
    """Goods actually received, which is what landed cost attaches to.

    A shipment and its costs allocate to nothing on their own: freight
    is spread across the goods it carried, and without the goods there
    is no denominator. This sheet is the goods.

    Fatal on a bad row, unlike Fitment, because a receipt moves stock
    and changes a valuation. A quantity that cannot be read is not a
    line somebody loses, it is a figure that would come out wrong.
    """
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    seen = set()
    for r, vals, _i in _rows(ws, h):
        grn = _text(vals[i["grn_number"]]) if "grn_number" in i else ""
        sku = _text(vals[i["sku"]]) if "sku" in i else ""
        branch = (_text(vals[i["branch_code"]]).upper()
                  if "branch_code" in i else "")
        if not (grn and sku and branch):
            p.problems.append(Problem(
                "Goods received", r,
                "Needs a GRN number, a SKU and a branch."))
            continue
        if "qty" not in i or not _is_num(vals[i["qty"]]):
            p.problems.append(Problem(
                "Goods received", r, f"{sku}: qty is not a number."))
            continue
        qty = _num(vals[i["qty"]])
        if qty <= 0:
            p.problems.append(Problem(
                "Goods received", r,
                f"{sku}: a receipt of {qty:g} is not a receipt. Leave the "
                f"row out, or record a count on the stock screen."))
            continue
        batch = _text(vals[i["batch"]]) if "batch" in i else ""
        # The batch is part of what identifies a line. One delivery
        # commonly carries the same reagent in two lots with different
        # expiry dates, and the database has always been able to hold
        # them -- this reader refused them, so the second lot was
        # reported as a mistake and dropped.
        key = (grn, sku, batch)
        if key in seen:
            p.problems.append(Problem(
                "Goods received", r,
                f"{sku}"
                f"{' batch ' + batch if batch else ''} appears twice on "
                f"{grn}. One row per lot, or the importer has to guess "
                f"which quantity arrived."))
            continue
        seen.add(key)
        p.receipts.append({
            "grn": grn,
            "shipment": (_text(vals[i["shipment_ref"]])
                         if "shipment_ref" in i else ""),
            "po": _text(vals[i["po_number"]]) if "po_number" in i else "",
            "branch": branch,
            "sku": sku,
            "qty": qty,
            "price": (_num(vals[i["unit_price"]])
                      if "unit_price" in i
                      and _is_num(vals[i["unit_price"]]) else 0),
            # Blank means "take the order's", which the database does
            # on insert. Defaulting to PKR here would quietly convert
            # a euro invoice at one to one.
            "currency": (_text(vals[i["currency"]]).upper()
                         if "currency" in i else "") or None,
            "fx": (_num(vals[i["fx_rate"]])
                   if "fx_rate" in i and _is_num(vals[i["fx_rate"]])
                   else None),
            "received": _day(vals[i["received_at"]])
                        if "received_at" in i else None,
            "weight": (_num(vals[i["weight_kg"]])
                       if "weight_kg" in i
                       and _is_num(vals[i["weight_kg"]]) else None),
            "volume": (_num(vals[i["volume_cbm"]])
                       if "volume_cbm" in i
                       and _is_num(vals[i["volume_cbm"]]) else None),
            "batch": batch,
            "expiry": _day(vals[i["expiry_date"]])
                      if "expiry_date" in i else None,
        })


def _read_fitment(ws, p):
    """Which reagent runs on which analyser.

    Two columns and a pairing, which is why it is a sheet rather than
    a column on Items: a reagent commonly fits several analysers, and
    a column would force somebody to pick one and be wrong.

    Not fatal when a row is wrong. Fitment makes a slow-moving line
    readable; it is not an input to any figure, so a typo in it should
    cost the person that line and not the import.
    """
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    seen = set()
    for r, vals, _i in _rows(ws, h):
        sku = _text(vals[i["sku"]]) if "sku" in i else ""
        machine = _text(vals[i["machine_key"]]) if "machine_key" in i else ""
        if not (sku and machine):
            p.problems.append(Problem(
                "Fitment", r, "Needs both a SKU and a machine key.",
                fatal=False))
            continue
        if (sku, machine) in seen:
            continue
        seen.add((sku, machine))
        p.fitment.append({"sku": sku, "machine": machine})


def _read_machines(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    seen = set()
    for r, vals, _i in _rows(ws, h):
        key = _text(vals[i.get("machine_key", 0)])
        if not key:
            continue
        if key in seen:
            p.problems.append(Problem("Machines", r,
                                      f"{key} appears more than once."))
            continue
        seen.add(key)
        p.machines.append({
            "key": key,
            "model": _text(vals[i["model"]]) if "model" in i else key,
            "serial": _text(vals[i["serial_no"]]) if "serial_no" in i else "",
            "customer": _text(vals[i["customer_code"]]) if "customer_code" in i else "",
            "branch": _text(vals[i["branch_code"]]).upper() if "branch_code" in i else "",
            "placed": _day(vals[i["placed_date"]]) if "placed_date" in i else None,
            "capital": _num(vals[i["capital_cost"]]) if "capital_cost" in i else 0,
            "months": int(_num(vals[i["contract_months"]], 36)) if "contract_months" in i else 36,
            "tests": _num(vals[i["expected_monthly_tests"]]) if "expected_monthly_tests" in i else 0,
            "per_test": _num(vals[i["revenue_per_test"]]) if "revenue_per_test" in i else 0,
            "margin": _num(vals[i["reagent_margin_pct"]]) if "reagent_margin_pct" in i else 0,
        })


def _read_jobs(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    seen = set()
    for r, vals, _i in _rows(ws, h):
        no = _text(vals[i.get("job_no", 0)])
        opened = _day(vals[i["opened_date"]]) if "opened_date" in i else None
        if not no or not opened:
            continue
        if no in seen:
            p.problems.append(Problem("Service jobs", r,
                                      f"{no} appears more than once."))
            continue
        seen.add(no)
        p.jobs.append({
            "no": no,
            "machine": _text(vals[i["machine_key"]]) if "machine_key" in i else "",
            "branch": _text(vals[i["branch_code"]]).upper() if "branch_code" in i else "",
            "opened": opened,
            "closed": _day(vals[i["closed_date"]]) if "closed_date" in i else None,
            "hours": _num(vals[i["labour_hours"]]) if "labour_hours" in i else 0,
            "billed": _num(vals[i["labour_hours_billed"]]) if "labour_hours_billed" in i else 0,
            "parts": _num(vals[i["parts_cost"]]) if "parts_cost" in i else 0,
            "charged": _num(vals[i["parts_charged"]]) if "parts_charged" in i else 0,
            "warranty": (_text(vals[i["is_warranty"]]).lower() in TRUE
                         if "is_warranty" in i else False),
            "repeat": (_text(vals[i["is_repeat_visit"]]).lower() in TRUE
                       if "is_repeat_visit" in i else False),
            "down": _num(vals[i["downtime_days"]]) if "downtime_days" in i else 0,
        })


# --------------------------------------------------- migration 0009
#
# One reader per new sheet. They follow the readers above exactly: a
# row with nothing in its key columns is a warning rather than a fatal
# problem, because a half-filled optional sheet should cost that row
# and not the import.


def _read_accounts(ws, p):
    """The trial balance.

    statement_line is checked against the engine's own list rather
    than guessed from the account name. Insurance could be an expense
    or a prepayment, and a product that guesses puts the difference in
    profit.
    """
    from engine.modules.finance import ALL_LINES

    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    for r, vals, _i in _rows(ws, h):
        code = _text(vals[i["account_code"]]) if "account_code" in i else ""
        line = (_text(vals[i["statement_line"]]).lower()
                if "statement_line" in i else "")
        if not code:
            continue
        if line not in ALL_LINES:
            p.problems.append(Problem(
                "Trial balance", r,
                f"{line or 'blank'} is not a statement line. One of: "
                f"{', '.join(ALL_LINES)}.", fatal=False))
            continue
        p.accounts.append({
            "code": code,
            "name": (_text(vals[i["account_name"]])
                     if "account_name" in i else code),
            "line": line,
            "from": _day(vals[i["period_from"]]) if "period_from" in i else None,
            "to": _day(vals[i["period_to"]]) if "period_to" in i else None,
            "opening": _num(vals[i["opening"]]) if "opening" in i else 0.0,
            "closing": _num(vals[i["closing"]]) if "closing" in i else 0.0,
        })


def _read_dividends(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    for r, vals, _i in _rows(ws, h):
        on = _day(vals[i["paid_on"]]) if "paid_on" in i else None
        amount = _num(vals[i["amount"]]) if "amount" in i else 0.0
        if not on or not amount:
            continue
        kind = (_text(vals[i["kind"]]).lower() if "kind" in i else "dividend")
        p.dividends.append({
            "kind": kind if kind in (
                "dividend", "capital_injection", "other") else "other",
            "on": on, "amount": amount,
            "note": _text(vals[i["note"]]) if "note" in i else "",
        })


COMMITMENT_KINDS = ("lc_settlement", "loan_repayment", "payroll", "rent",
                    "tax", "supplier", "other")


def _read_commitments(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    for r, vals, _i in _rows(ws, h):
        label = _text(vals[i["label"]]) if "label" in i else ""
        due = _day(vals[i["due_date"]]) if "due_date" in i else None
        if not (label and due):
            p.problems.append(Problem(
                "Committed payments", r,
                "Needs a label and a due date.", fatal=False))
            continue
        kind = (_text(vals[i["kind"]]).lower() if "kind" in i else "other")
        if kind not in COMMITMENT_KINDS:
            p.problems.append(Problem(
                "Committed payments", r,
                f"{kind or 'blank'} is not a kind. One of: "
                f"{', '.join(COMMITMENT_KINDS)}.", fatal=False))
            continue
        p.commitments.append({
            "label": label, "kind": kind, "due": due,
            "amount": _num(vals[i["amount"]]) if "amount" in i else 0.0,
            "recurring": _yes(vals[i["is_recurring"]])
                         if "is_recurring" in i else False,
            "every": int(_num(vals[i["recurs_every_days"]]))
                     if "recurs_every_days" in i else 0,
            "branch": (_text(vals[i["branch_code"]]).upper()
                       if "branch_code" in i else ""),
        })


def _read_bank(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    for r, vals, _i in _rows(ws, h):
        on = _day(vals[i["as_at"]]) if "as_at" in i else None
        if not on:
            continue
        p.bank.append({
            "as_at": on,
            "balance": _num(vals[i["balance"]]) if "balance" in i else 0.0,
            "account": (_text(vals[i["account_label"]])
                        if "account_label" in i else "") or "Main account",
        })


def _read_employees(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    for r, vals, _i in _rows(ws, h):
        no = _text(vals[i["employee_no"]]) if "employee_no" in i else ""
        name = _text(vals[i["name"]]) if "name" in i else ""
        if not (no and name):
            p.problems.append(Problem(
                "Employees", r, "Needs a number and a name.", fatal=False))
            continue
        row = {
            "no": no, "name": name,
            "dept": _text(vals[i["department"]]) if "department" in i else "",
            "branch": (_text(vals[i["branch_code"]]).upper()
                       if "branch_code" in i else ""),
            "basic": _num(vals[i["basic_monthly"]])
                     if "basic_monthly" in i else 0.0,
            "allow": _num(vals[i["allowances_monthly"]])
                     if "allowances_monthly" in i else 0.0,
            "medical": _num(vals[i["medical_monthly"]])
                       if "medical_monthly" in i else 0.0,
            "pf_er": _num(vals[i["employer_pf_rate"]])
                     if "employer_pf_rate" in i else 0.0,
            "pf_ee": _num(vals[i["employee_pf_rate"]])
                     if "employee_pf_rate" in i else 0.0,
            "joined": _day(vals[i["joined"]]) if "joined" in i else None,
        }
        # A provident rate typed as 5 for five per cent is the
        # commonest way to get a payroll figure wrong by twenty times.
        # Refused rather than divided by a hundred: converting it would
        # be the importer deciding what somebody meant, and the error
        # would surface months later in a figure nobody can explain.
        for key, label in (("pf_er", "Employer"), ("pf_ee", "Employee")):
            if row[key] > 1:
                p.problems.append(Problem(
                    "Employees", r,
                    f"{label} provident rate is a fraction: five per "
                    f"cent is 0.05, not {row[key]:g}.", fatal=False))
                row[key] = 0.0
        p.employees.append(row)


PIPELINE_STAGES = ("lead", "qualified", "quoted", "tender_submitted",
                   "won", "lost")


def _read_pipeline(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    for r, vals, _i in _rows(ws, h):
        ref = _text(vals[i["reference"]]) if "reference" in i else ""
        stage = _text(vals[i["stage"]]).lower() if "stage" in i else ""
        if not ref:
            continue
        if stage not in PIPELINE_STAGES:
            p.problems.append(Problem(
                "Pipeline", r,
                f"{stage or 'blank'} is not a stage. One of: "
                f"{', '.join(PIPELINE_STAGES)}.", fatal=False))
            continue
        p.pipeline.append({
            "ref": ref, "stage": stage,
            "customer": (_text(vals[i["customer_code"]]).upper()
                         if "customer_code" in i else ""),
            "value": _num(vals[i["value"]]) if "value" in i else 0.0,
            "close": _day(vals[i["expected_close"]])
                     if "expected_close" in i else None,
            "sku": _text(vals[i["sku"]]) if "sku" in i else "",
            "qty": _num(vals[i["quantity"]]) if "quantity" in i else 0.0,
            "source": _text(vals[i["source"]]) if "source" in i else "",
            "owner": _text(vals[i["owner"]]) if "owner" in i else "",
            "opened": _day(vals[i["opened"]]) if "opened" in i else None,
            "closed": _day(vals[i["closed"]]) if "closed" in i else None,
        })


def _read_spend(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    for r, vals, _i in _rows(ws, h):
        label = _text(vals[i["label"]]) if "label" in i else ""
        on = _day(vals[i["spent_on"]]) if "spent_on" in i else None
        if not (label and on):
            continue
        p.spend.append({
            "label": label, "on": on,
            "category": (_text(vals[i["category"]])
                         if "category" in i else "") or "other",
            "amount": _num(vals[i["amount"]]) if "amount" in i else 0.0,
        })


def _read_contacts(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    for r, vals, _i in _rows(ws, h):
        name = _text(vals[i["name"]]) if "name" in i else ""
        if not name:
            continue
        customer = (_text(vals[i["customer_code"]]).upper()
                    if "customer_code" in i else "")
        supplier = (_text(vals[i["supplier_name"]])
                    if "supplier_name" in i else "")
        # Exactly one owner, which the table also enforces. Both or
        # neither leaves "whose contact is this" with no answer.
        if bool(customer) == bool(supplier):
            p.problems.append(Problem(
                "Contacts", r,
                "Give either a customer code or a supplier name, not "
                "both and not neither.", fatal=False))
            continue
        p.contacts.append({
            "customer": customer, "supplier": supplier, "name": name,
            "role": _text(vals[i["role"]]) if "role" in i else "",
            "email": _text(vals[i["email"]]) if "email" in i else "",
            "phone": _text(vals[i["phone"]]) if "phone" in i else "",
            "whatsapp": _text(vals[i["whatsapp"]]) if "whatsapp" in i else "",
            "primary": _yes(vals[i["is_primary"]]) if "is_primary" in i else False,
        })


CHANNELS = ("note", "call", "visit", "email", "whatsapp", "meeting")


def _read_dealings(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    for r, vals, _i in _rows(ws, h):
        code = (_text(vals[i["customer_code"]]).upper()
                if "customer_code" in i else "")
        on = _day(vals[i["happened_on"]]) if "happened_on" in i else None
        summary = _text(vals[i["summary"]], ) if "summary" in i else ""
        if not (code and on and summary):
            p.problems.append(Problem(
                "Dealings", r,
                "Needs a customer code, a date and a summary.",
                fatal=False))
            continue
        channel = _text(vals[i["channel"]]).lower() if "channel" in i else ""
        p.dealings.append({
            "customer": code, "on": on, "summary": summary,
            "channel": channel if channel in CHANNELS else "note",
            "next": _text(vals[i["next_step"]]) if "next_step" in i else "",
            "next_on": _day(vals[i["next_step_on"]])
                       if "next_step_on" in i else None,
        })


def _cross_check(p: Plan):
    """The checks that need more than one sheet.

    A SKU that is on Stock and not on Items is the commonest real
    import error, and the one with the worst silent outcome: skipped
    quietly, the business's stock total is wrong and nothing says so.
    """
    skus = {i["sku"] for i in p.items}
    codes = {b["code"] for b in p.branches}

    for s in p.stock:
        if s["sku"] not in skus:
            p.problems.append(Problem(
                "Stock", None,
                f"{s['sku']} is not on the Items sheet."))
        if s["branch"] not in codes:
            p.problems.append(Problem(
                "Stock", None,
                f"{s['branch']} is not on the Branches sheet."))

    for hrow in p.history:
        if hrow["sku"] not in skus or hrow["branch"] not in codes:
            p.problems.append(Problem(
                "Sales history", None,
                f"{hrow['sku']} at {hrow['branch']} is not set up.",
                fatal=False))
            break           # one is enough; the detail is on Stock

    for o in p.orders:
        if o["sku"] not in skus:
            p.problems.append(Problem(
                "On order", None,
                f"{o['sku']} is not on the Items sheet.", fatal=False))

    codes_c = {c["code"] for c in p.customers}
    for inv in p.invoices:
        if inv["customer"] not in codes_c:
            p.problems.append(Problem(
                "Invoices", None,
                f"{inv['no']}: customer {inv['customer']} is not on the "
                f"Customers sheet."))
            break
    keys_m = {m["key"] for m in p.machines}
    for j in p.jobs:
        if j["machine"] and j["machine"] not in keys_m:
            p.problems.append(Problem(
                "Service jobs", None,
                f"{j['no']}: machine {j['machine']} is not on the Machines "
                f"sheet.", fatal=False))
            break

    without = skus - {s["sku"] for s in p.stock}
    if without:
        p.problems.append(Problem(
            "Items", None,
            f"{len(without)} items have no stock row. They will be created "
            f"with nothing on the shelf.", fatal=False))

    if not p.history:
        p.problems.append(Problem(
            "Sales history", None,
            "No sales history. Items will import, but with no demand there "
            "is no cover, no reorder point and nothing to decide.",
            fatal=False))


# ------------------------------------------------------------- writing

def supplier_code(name: str) -> str:
    """A stable code from a name, because the sheet only asks for a name.

    Deterministic on purpose: importing the same file twice has to land
    on the same supplier rather than making a second one.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").upper()
    return slug[:24] or "SUPPLIER"

def apply(conn, p: Plan, as_of: date | None = None) -> dict:
    """Write the plan. One transaction, owned by the caller.

    The caller holds the transaction so that a failure anywhere leaves
    nothing behind. A partly-written import is worse than a refused
    one: the retry then collides with what the first attempt made, and
    somebody reconciles two half-states by hand.

    The bulk tables go through executemany, which psycopg pipelines
    into a few round trips rather than one per row. That is not a
    micro-optimisation. A statement at a time, this sent 5,218 of them;
    over a unix socket in a test that is two seconds, and across the
    network to a hosted database it is two to five minutes -- long
    enough that the platform cuts the request and returns a 502 with no
    body, which is exactly how this first failed in production.
    """
    if not p.ok:
        raise BadWorkbook("This plan has problems that must be fixed first.")

    # A connection or a cursor, because both arrive here: the route
    # passes the connection it got from as_user, the tests pass the
    # cursor they are already holding. Resolving it once beats every
    # call site having to know, and the difference between the two is
    # how the first version of this passed its tests and failed in
    # production.
    run = conn.cursor() if hasattr(conn, "cursor") else conn

    # Which organisation, asked once. Under row-level security this
    # returns the caller's own; with none it returns nothing, and every
    # insert below would write nowhere and hand back None.
    row = run.execute(
        "select id from public.organisations order by created_at limit 1"
    ).fetchone()
    if not row:
        raise NoOrganisation(
            "This account has no organisation yet, so there is nowhere to "
            "put the import. Name your business first.")
    org = row[0]

    as_of = as_of or date.today()
    made = {k: 0 for k in
            ("branches", "suppliers", "items", "movements", "history",
             "orders", "shipments", "customers", "invoices", "machines",
             "service_jobs")}

    # ---- the small tables, which need their ids back

    loc = {}
    for b in p.branches:
        got = run.execute(
            "insert into public.locations (org_id, code, name, city, "
            "is_warehouse) values (%s, %s, %s, %s, %s) "
            "on conflict (org_id, code) do update set name = excluded.name "
            "returning id",
            (org, b["code"], b["name"], b["city"] or None, b["warehouse"]),
        ).fetchone()
        loc[b["code"]] = got[0]
        made["branches"] += 1

    names = {s["name"] for s in p.suppliers}
    names |= {i["supplier"] for i in p.items if i["supplier"]}
    sup = {}
    for name in sorted(n for n in names if n):
        spec = next((s for s in p.suppliers if s["name"] == name), None)
        got = run.execute(
            "insert into public.suppliers (org_id, code, name, country, "
            "currency) values (%s, %s, %s, %s, %s) "
            "on conflict (org_id, code) do update set name = excluded.name "
            "returning id",
            (org, supplier_code(name), name,
             (spec or {}).get("country") or None,
             _three((spec or {}).get("currency") or "")),
        ).fetchone()
        sup[name] = got[0]
        made["suppliers"] += 1

    item = {}
    for i in p.items:
        got = run.execute(
            "insert into public.items (org_id, sku, description, category, "
            "uom, supplier_id, lead_time_days, lead_time_days_std, "
            "is_clinical_critical, list_price, is_generic) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (org_id, sku) do update set "
            "description = excluded.description, category = excluded.category, "
            "lead_time_days = excluded.lead_time_days, "
            "lead_time_days_std = excluded.lead_time_days_std, "
            "is_clinical_critical = excluded.is_clinical_critical, "
            # A blank price in a re-import leaves the price that is
            # there. Somebody correcting lead times in a spreadsheet
            # that predates the price column should not silently wipe
            # every price in the catalogue.
            "list_price = coalesce(excluded.list_price, items.list_price), "
            "is_generic = excluded.is_generic "
            "returning id",
            (org, i["sku"], i["description"], i["category"], i["uom"],
             sup.get(i["supplier"]), i["lead"], i["lead_std"], i["critical"],
             i.get("price"), i.get("generic", False)),
        ).fetchone()
        item[i["sku"]] = got[0]
        made["items"] += 1

    # ---- the bulk tables, batched

    places, movements = [], []
    for s in p.stock:
        iid, lid = item.get(s["sku"]), loc.get(s["branch"])
        if not iid or not lid:
            continue
        places.append((org, iid, lid, s["expiry"]))
        # A zero opening places the line at the branch -- which the row
        # above does -- but writes no movement. public.stock_movement
        # forbids a zero quantity, and rightly: a movement of nothing
        # is not an event, and an append-only ledger should not carry
        # no-op rows to make a count come out even.
        #
        # The preview has to agree with this, and for a long time it
        # did not. See _specs: it counted every Stock row as an
        # incoming movement, so an out-of-stock line had no client_ref
        # to match against and was reported as new on every single
        # re-import. One empty shelf at one branch was enough to stop
        # a file ever reading as a replay.
        if s["qty"]:
            movements.append((
                org, iid, lid, s["qty"], s["cost"] or None, as_of,
                f"import:opening:{s['sku']}:{s['branch']}"))

    if places:
        run.executemany(
            "insert into public.item_locations (org_id, item_id, "
            "location_id, expiry_date) values (%s, %s, %s, %s) "
            "on conflict (item_id, location_id) do update set "
            "expiry_date = excluded.expiry_date", places)

    if movements:
        # Stock arrives as a ledger entry, not a balance. A stored
        # balance is a second number that can disagree with the
        # movements behind it, and then nobody can say which is wrong.
        # `do nothing`, against the unique index on (org_id,
        # client_ref). The ledger is append-only, so a replayed opening
        # movement must not be written twice and must not be edited
        # either -- ignoring it is the only correct outcome.
        #
        # Without the clause a second import raised a UniqueViolation
        # that aborted the whole transaction. Nothing was doubled, so
        # the figures were safe, but what reached the person was the
        # text of a Postgres error and what reached the log looked like
        # a bug rather than somebody clicking twice.
        #
        # The count is what was actually inserted, not what was
        # offered, so a replay reports zero movements rather than
        # claiming to have written rows it skipped.
        before = run.execute(
            "select count(*) from public.stock_movement"
            " where org_id = %s and client_ref like 'import:opening:%%'",
            (org,)).fetchone()[0]
        run.executemany(
            "insert into public.stock_movement (org_id, item_id, "
            "location_id, qty, unit_cost, reason, occurred_at, client_ref) "
            "values (%s, %s, %s, %s, %s, 'opening', %s, %s) "
            # The index is partial -- `where client_ref is not null`,
            # because only imported rows carry one -- and a partial
            # unique index can only arbitrate a conflict when the
            # statement repeats its predicate. Without the `where` the
            # database answers that no constraint matches, which reads
            # like a missing index rather than a missing clause.
            "on conflict (org_id, client_ref) where client_ref is not null "
            "do nothing", movements)
        after = run.execute(
            "select count(*) from public.stock_movement"
            " where org_id = %s and client_ref like 'import:opening:%%'",
            (org,)).fetchone()[0]
        made["movements"] = after - before
        made["movements_already_recorded"] = len(movements) - (after - before)

    history = []
    for h in p.history:
        iid, lid = item.get(h["sku"]), loc.get(h["branch"])
        if iid and lid and PERIOD.match(h["period"]):
            history.append((org, lid, iid, h["period"], h["qty"]))
    if history:
        run.executemany(
            "insert into public.demand_history (org_id, location_id, "
            "item_id, period, quantity, source) "
            "values (%s, %s, %s, %s, %s, 'import') "
            "on conflict (org_id, location_id, item_id, period) "
            "do update set quantity = excluded.quantity", history)
        made["history"] = len(history)

    # ---- purchase orders: header once, a line per row

    heads, lines = {}, []
    po_supplier: dict = {}
    for o in p.orders:
        iid, lid = item.get(o["sku"]), loc.get(o["branch"])
        if not iid or not lid:
            continue
        if o["po"] not in heads:
            got = run.execute(
                "insert into public.purchase_orders (org_id, location_id, "
                "supplier_id, po_number, currency, fx_rate, status, "
                "ordered_at, expected_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "on conflict (org_id, po_number) do update set "
                "status = excluded.status returning id",
                (org, lid, sup.get(o["supplier"]), o["po"], o["currency"],
                 o["fx"] or 1, o["status"], o["ordered"], o["expected"]),
            ).fetchone()
            heads[o["po"]] = got[0]
            # Remembered so a shipment can inherit the supplier from
            # the order it carries: the Shipments sheet names a PO, not
            # a supplier, because that is what a person has in front of
            # them.
            po_supplier[o["po"]] = sup.get(o["supplier"])
            made["orders"] += 1
        lines.append((org, heads[o["po"]], iid, o["qty"], o["price"]))
    if lines:
        # Against the natural key migration 0010 adds. Without it this
        # table had no constraint and no conflict clause, and did not
        # double only because the ledger aborted the transaction first
        # -- luck, not design. A workbook with an empty stock sheet
        # would have doubled every ordered quantity, which feeds the
        # stock position and the order sequencing, so it would have
        # surfaced as the engine recommending against stock that was
        # never on order.
        run.executemany(
            "insert into public.po_lines (org_id, po_id, item_id, qty, "
            "unit_price) values (%s, %s, %s, %s, %s) "
            "on conflict (org_id, po_id, item_id) do update set "
            "qty = excluded.qty, unit_price = excluded.unit_price", lines)

    # ---- customers, and what they owe

    cust = {}
    for c in p.customers:
        got = run.execute(
            "insert into public.customers (org_id, code, name, city, "
            "is_public_sector, is_intercompany, payment_terms_days, "
            "contact_name, contact_email) values (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (org_id, code) do update set name = excluded.name, "
            "is_public_sector = excluded.is_public_sector, "
            "is_intercompany = excluded.is_intercompany returning id",
            (org, c["code"], c["name"], c["city"] or None, c["public"],
             c["inter"], c["terms"], c["contact"] or None,
             c["email"] or None)).fetchone()
        cust[c["code"]] = got[0]
        made["customers"] += 1

    bills = []
    for inv in p.invoices:
        cid = cust.get(inv["customer"])
        if not cid:
            continue
        bills.append((org, cid, loc.get(inv["branch"]), inv["no"],
                      inv["date"], inv["due"], inv["amount"], inv["paid"]))
    if bills:
        run.executemany(
            "insert into public.invoices (org_id, customer_id, location_id, "
            "invoice_no, invoice_date, due_date, amount, amount_paid) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (org_id, invoice_no) do update set "
            "amount = excluded.amount, amount_paid = excluded.amount_paid",
            bills)
        made["invoices"] = len(bills)

    # ---- machines placed, and the jobs done on them

    mach = {}
    for m in p.machines:
        got = run.execute(
            "insert into public.machines (org_id, customer_id, location_id, "
            "machine_key, model, serial_no, placed_date, capital_cost, "
            "contract_months, expected_monthly_tests, revenue_per_test, "
            "reagent_margin_pct) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (org_id, machine_key) do update set "
            "model = excluded.model, capital_cost = excluded.capital_cost "
            "returning id",
            (org, cust.get(m["customer"]), loc.get(m["branch"]), m["key"],
             m["model"], m["serial"] or None, m["placed"], m["capital"],
             m["months"], m["tests"], m["per_test"], m["margin"])).fetchone()
        mach[m["key"]] = got[0]
        made["machines"] += 1

    # ---- which reagent runs on which analyser
    #
    # After both, because it is a pairing of two ids rather than a
    # record of its own. A SKU or a machine key that is not in this
    # workbook is reported and skipped rather than failing the import:
    # fitment is not an input to any figure, so a typo in it should
    # cost that line and not the whole file.
    links, unknown = [], []
    for f in p.fitment:
        iid, mid = item.get(f["sku"]), mach.get(f["machine"])
        if iid is None or mid is None:
            unknown.append(f["sku"] if iid is None else f["machine"])
            continue
        links.append((org, iid, mid))
    if links:
        run.executemany(
            "insert into public.item_machines (org_id, item_id, machine_id) "
            "values (%s,%s,%s) on conflict do nothing",
            links)
        made["fitment"] = len(links)
    if unknown:
        missing = sorted(set(unknown))
        shown = ", ".join(missing[:6])
        p.problems.append(Problem(
            "Fitment", None,
            f"The Fitment sheet names {len(missing)} "
            f"{'thing' if len(missing) == 1 else 'things'} that "
            f"{'is' if len(missing) == 1 else 'are'} not in this workbook "
            f"({shown}{', and others' if len(missing) > 6 else ''}). "
            f"Those pairings were skipped; nothing else was affected.",
            fatal=False))

    visits = []
    for j in p.jobs:
        visits.append((org, mach.get(j["machine"]), loc.get(j["branch"]),
                       j["no"], j["opened"], j["closed"], j["hours"],
                       j["billed"], j["parts"], j["charged"], j["warranty"],
                       j["repeat"], j["down"]))
    if visits:
        run.executemany(
            "insert into public.service_jobs (org_id, machine_id, "
            "location_id, job_no, opened_date, closed_date, labour_hours, "
            "labour_hours_billed, parts_cost, parts_charged, is_warranty, "
            "is_repeat_visit, downtime_days) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (org_id, job_no) do update set "
            "labour_hours = excluded.labour_hours, "
            "labour_hours_billed = excluded.labour_hours_billed",
            visits)
        made["service_jobs"] = len(visits)

    # ---- shipments, and what landed on top of them
    #
    # Read since the sheet was added and never written: `made` carried
    # a "shipments" key that nothing ever set, so it reported zero and
    # looked like an empty sheet. The freight module could therefore
    # only ever be filled by hand.
    #
    # The sheet is one row per cost line, so the shipments themselves
    # are the distinct references in it.
    # Declared out here because the receipts below link to it. A
    # workbook with goods received and no Shipments sheet is a normal
    # thing -- stock arrives without freight you are tracking -- and
    # it should import, not raise a NameError.
    ship_ids: dict = {}
    if p.shipments:
        ships = {}
        for sh in p.shipments:
            keep = ships.setdefault(sh["ref"], sh)
            # The first row that names a mode or a date wins; later
            # rows for the same shipment are its other cost lines and
            # usually repeat them.
            for key in ("mode", "etd", "eta", "po"):
                keep[key] = keep.get(key) or sh.get(key)

        for ref, sh in ships.items():
            got = run.execute(
                "insert into public.shipments (org_id, reference,"
                " supplier_id, mode, etd, eta, status)"
                " values (%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (org_id, reference) do update set"
                " mode = excluded.mode, etd = excluded.etd,"
                " eta = excluded.eta returning id",
                (org, ref, po_supplier.get(sh.get("po")),
                 sh.get("mode") or None, sh.get("etd"), sh.get("eta"),
                 "arrived" if sh.get("eta") and sh["eta"] <= as_of
                 else "in_transit"),
            ).fetchone()
            ship_ids[ref] = got[0]

        # Replaced rather than added to. A cost line has no natural key
        # -- two freight charges on one shipment are two real rows --
        # so a re-import would otherwise double every landed cost, and
        # the uplift on every item with it.
        run.executemany(
            "delete from public.shipment_costs where shipment_id = %s",
            [(i,) for i in ship_ids.values()])
        run.executemany(
            "insert into public.shipment_costs (org_id, shipment_id,"
            " kind, amount, currency, fx_rate, allocate_by, note)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s)",
            [(org, ship_ids[c["ref"]], c["kind"], c["amount"],
              c["currency"], c["fx"] or 1,
              c["allocate_by"], c["cost_type"] or None)
             for c in p.shipments])
        made["shipments"] = len(ships)
        made["shipment_costs"] = len(p.shipments)

    # ---- goods received, which is where landed cost becomes a cost
    #
    # Shipments and their costs were being imported and allocating to
    # nothing. The allocation divides a cost across the goods that
    # shipment carried, and nothing was creating the goods: a complete
    # workbook produced four shipments, sixteen cost lines and zero
    # rows in public.landed_cost. Every figure that reads a landed
    # unit cost silently fell back to the invoice price, which is the
    # one thing this product exists not to do.
    #
    # Three tables, in order, because each needs the one before it.
    # The grn_line insert fires a trigger that posts the stock
    # movement and another that inherits the order's currency and
    # rate, so a euro receipt against a euro order needs no rate typed
    # on the sheet.
    if p.receipts:
        # A receipt is a fact about a day, so a re-import must not
        # write it twice -- and unlike a cost line it has a natural
        # key, the GRN number, so it does not need deleting first.
        grns, grn_new = {}, 0
        for g in p.receipts:
            lid = loc.get(g["branch"])
            if not lid or g["grn"] in grns:
                continue
            got = run.execute(
                "insert into public.goods_receipts (org_id, location_id,"
                " po_id, grn_number, received_at)"
                " values (%s,%s,%s,%s,coalesce(%s, current_date))"
                " on conflict (org_id, grn_number) do update set"
                " received_at = excluded.received_at returning id,"
                " (xmax = 0) as inserted",
                (org, lid, heads.get(g["po"]), g["grn"], g["received"]),
            ).fetchone()
            grns[g["grn"]] = got[0]
            grn_new += 1 if got[1] else 0

        # Which po_line a receipt line is against, so the trigger can
        # take the order's currency and rate. Looked up by item rather
        # than carried on the sheet: a person has a GRN and a SKU in
        # front of them, not a line id.
        po_line = {}
        if heads:
            for pid, iid, lid2 in run.execute(
                "select po_id, item_id, id from public.po_lines"
                " where org_id = %s", (org,)).fetchall():
                po_line[(pid, iid)] = lid2

        made["goods_receipts"] = grn_new
        received = 0
        for g in p.receipts:
            gid, iid = grns.get(g["grn"]), item.get(g["sku"])
            if not gid or not iid:
                continue
            # Skipped rather than doubled on a re-import. The movement
            # this line's trigger posts is append-only, so there is no
            # safe upsert: the only correct answer for a line already
            # recorded is to leave it alone.
            #
            # This asked the wrong question. It was guarded by
            # `not grn_new`, a counter of new receipts across the whole
            # workbook -- so the moment any one receipt was new, the
            # check was switched off for every other line in the file
            # and the ones already recorded were inserted a second
            # time. A customer who keeps one workbook and adds this
            # week's receipts to it doubled every receipt they had
            # imported before, and the trigger posted the stock to
            # match. 154 units became 308 on the second upload.
            #
            # The guard was never needed either. A genuinely new
            # receipt has no lines yet, so this query finds nothing and
            # the insert goes ahead on its own.
            #
            # Identity is the receipt, the item and the batch. Two lots
            # of one SKU on one delivery are two real lines with two
            # expiry dates, and keying on the item alone would have
            # silently dropped the second.
            if run.execute(
                    "select 1 from public.grn_lines"
                    " where grn_id = %s and item_id = %s"
                    "   and coalesce(batch, '') = coalesce(%s, '')"
                    " limit 1",
                    (gid, iid, g["batch"] or None)).fetchone():
                continue
            got = run.execute(
                "insert into public.grn_lines (org_id, grn_id, po_line_id,"
                " item_id, qty, unit_price, currency, fx_rate,"
                " expiry_date, batch)"
                " values (%s,%s,%s,%s,%s,%s,%s,coalesce(%s,1),%s,%s)"
                " returning id",
                (org, gid, po_line.get((heads.get(g["po"]), iid)), iid,
                 g["qty"], g["price"], g["currency"], g["fx"],
                 g["expiry"], g["batch"] or None),
            ).fetchone()
            received += 1
            ship = ship_ids.get(g["shipment"]) if g["shipment"] else None
            if ship:
                run.execute(
                    "insert into public.shipment_lines (org_id, shipment_id,"
                    " grn_line_id, weight_kg, volume_cbm)"
                    " values (%s,%s,%s,%s,%s)"
                    " on conflict (shipment_id, grn_line_id) do nothing",
                    (org, ship, got[0], g["weight"], g["volume"]))
            elif g["shipment"]:
                p.problems.append(Problem(
                    "Goods received", None,
                    f"{g['grn']} names shipment {g['shipment']}, which is "
                    f"not on the Shipments sheet. The goods were received; "
                    f"the freight on them could not be spread.",
                    fatal=False))
        made["received_lines"] = received

    # ---- a shipment whose costs land on nothing
    #
    # The check that would have caught the hole this sheet was added
    # to fill. A shipment with costs and no lines is not an error the
    # database can refuse -- every row is valid on its own -- and it
    # produces no exception, no empty screen and no obviously wrong
    # number. It produces a freight bill that is silently spread
    # across zero goods, so every item keeps its invoice price and
    # the Landing screen shows a total nothing is using.
    #
    # Said at import, because that is the moment somebody can fix it
    # by adding the rows.
    if ship_ids:
        stranded = run.execute(
            "select s.reference, count(distinct c.id)"
            "  from public.shipments s"
            "  join public.shipment_costs c on c.shipment_id = s.id"
            "  left join public.shipment_lines l on l.shipment_id = s.id"
            " where s.org_id = %s and l.grn_line_id is null"
            " group by s.reference order by s.reference", (org,)).fetchall()
        if stranded:
            refs = ", ".join(r[0] for r in stranded[:5])
            total = sum(r[1] for r in stranded)
            p.problems.append(Problem(
                "Shipments", None,
                f"{len(stranded)} "
                f"{'shipment carries' if len(stranded) == 1 else 'shipments carry'}"
                f" {total} cost "
                f"{'line' if total == 1 else 'lines'} and no goods "
                f"({refs}{', and others' if len(stranded) > 5 else ''}). "
                f"Freight and duty are spread across what a shipment "
                f"carried, so until those lines are on the Goods received "
                f"sheet these costs reach no item and every price stays "
                f"at the supplier's invoice.",
                fatal=False))

    # ---- migration 0009's tables
    #
    # Each one upserts on the natural key the sheet already has, so a
    # second import of the same workbook corrects rather than
    # duplicates. The two without a natural key -- marketing spend and
    # dealings -- are handled below, and the reason is spelled out
    # there rather than left as an inconsistency.

    if p.accounts:
        made["accounts"] = _write(run, org, "public.accounts",
            ("code", "name", "line", "period_from", "period_to",
             "opening", "closing"),
            [(a["code"], a["name"], a["line"],
              a["from"] or date(as_of.year, 1, 1), a["to"] or as_of,
              a["opening"], a["closing"]) for a in p.accounts],
            conflict="(org_id, code, period_to)",
            update=("name", "line", "period_from", "opening", "closing"))

    if p.dividends:
        made["dividends"] = _write(run, org, "public.equity_movements",
            ("kind", "moved_on", "amount", "note"),
            [(d["kind"], d["on"], d["amount"], d["note"] or None)
             for d in p.dividends],
            conflict="(org_id, kind, moved_on, amount)",
            update=("note",))

    if p.commitments:
        made["commitments"] = _write(run, org, "public.commitments",
            ("location_id", "label", "kind", "due_date", "amount",
             "is_recurring", "recurs_every_days"),
            [(loc.get(c["branch"]), c["label"], c["kind"], c["due"],
              c["amount"], c["recurring"], c["every"] or 0)
             for c in p.commitments],
            conflict="(org_id, label, due_date)",
            update=("kind", "amount", "is_recurring",
                    "recurs_every_days", "location_id"))

    if p.bank:
        made["bank_balances"] = _write(run, org, "public.cash_balances",
            ("as_at", "balance", "account_label"),
            [(b["as_at"], b["balance"], b["account"]) for b in p.bank],
            conflict="(org_id, as_at, account_label)",
            update=("balance",))

    if p.employees:
        made["employees"] = _write(run, org, "public.employees",
            ("location_id", "employee_no", "name", "department",
             "basic_monthly", "allowances_monthly", "medical_monthly",
             "employer_pf_rate", "employee_pf_rate", "joined"),
            [(loc.get(e["branch"]), e["no"], e["name"], e["dept"],
              e["basic"], e["allow"], e["medical"], e["pf_er"],
              e["pf_ee"], e["joined"]) for e in p.employees],
            conflict="(org_id, employee_no)",
            update=("name", "department", "basic_monthly",
                    "allowances_monthly", "medical_monthly",
                    "employer_pf_rate", "employee_pf_rate"))

    if p.pipeline:
        made["pipeline"] = _write(run, org, "public.opportunities",
            ("customer_id", "item_id", "reference", "stage", "value",
             "expected_close", "quantity", "source", "owner_name",
             "opened", "closed"),
            [(cust.get(o["customer"]), item.get(o["sku"]), o["ref"],
              o["stage"], o["value"], o["close"], o["qty"], o["source"],
              o["owner"], o["opened"], o["closed"])
             for o in p.pipeline],
            conflict="(org_id, reference)",
            update=("stage", "value", "expected_close", "quantity",
                    "owner_name", "closed"))

    if p.spend:
        made["marketing_spend"] = _write(run, org, "public.marketing_spend",
            ("label", "category", "spent_on", "amount"),
            [(x["label"], x["category"], x["on"], x["amount"])
             for x in p.spend],
            conflict="(org_id, label, spent_on)",
            update=("category", "amount"))

    if p.contacts:
        sup_by_name = {n: i for n, i in sup.items()}
        made["contacts"] = _write(run, org, "public.contacts",
            ("customer_id", "supplier_id", "name", "role_title", "email",
             "phone", "whatsapp", "is_primary"),
            [(cust.get(c["customer"]), sup_by_name.get(c["supplier"]),
              c["name"], c["role"], c["email"] or None,
              c["phone"] or None, c["whatsapp"] or None, c["primary"])
             for c in p.contacts
             if cust.get(c["customer"]) or sup_by_name.get(c["supplier"])],
            conflict="(org_id, coalesce(customer_id, supplier_id), name)",
            update=("role_title", "email", "phone", "whatsapp",
                    "is_primary"))

    if p.dealings:
        made["dealings"] = _write(run, org, "public.interactions",
            ("customer_id", "happened_on", "channel", "summary",
             "next_step", "next_step_on"),
            [(cust.get(d["customer"]), d["on"], d["channel"], d["summary"],
              d["next"] or None, d["next_on"])
             for d in p.dealings if cust.get(d["customer"])],
            conflict="(org_id, coalesce(customer_id, supplier_id),"
                     " happened_on, md5(summary))",
            update=("channel", "next_step", "next_step_on"))

    return made


def _write(run, org, table, cols, rows, conflict=None, update=()):
    """One bulk insert, optionally upserting. Returns rows offered.

    Pulled out because nine tables wanted the same six lines, and six
    lines copied nine times is nine places for a column list and a
    value tuple to drift apart.

    `executemany` rather than a statement each: psycopg pipelines it
    into a few round trips. That is not a micro-optimisation -- this
    importer once sent 5,218 individual statements and the platform
    cut the request at two minutes.
    """
    if not rows:
        return 0
    names = ", ".join(("org_id", *cols))
    marks = ", ".join(["%s"] * (len(cols) + 1))
    sql = f"insert into {table} ({names}) values ({marks})"
    if conflict:
        sets = ", ".join(f"{c} = excluded.{c}" for c in update)
        sql += (f" on conflict {conflict} do update set {sets}"
                if sets else f" on conflict {conflict} do nothing")
    run.executemany(sql, [(org, *r) for r in rows])
    return len(rows)


# ----------------------------------------------------- what it will do
#
# A preview that counts rows tells somebody how big their file is. It
# does not tell them the thing they actually want to know before
# pressing commit, which is whether this is the same file again.
#
# So every table is compared against what is already recorded, on the
# natural key the import matches on, and each incoming row comes back
# as one of three things: new, already recorded and identical, or
# already recorded and different. The third is the one worth seeing --
# a corrected workbook is mostly unchanged rows with a handful of edits
# buried in it, and "42 unchanged, 2 changed" is a different decision
# from "44 unchanged".


def _norm(v):
    """One comparable form, so a float and a Decimal are not 'changed'.

    Numbers out of Postgres are Decimal, out of a spreadsheet are
    float, and comparing them directly reports every row as different.
    Four places because that is what the columns hold.
    """
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 4)
    if isinstance(v, Decimal):
        return round(float(v), 4)
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    return str(v).strip()


def _cmp(row: tuple) -> tuple:
    return tuple(_norm(v) for v in row)


@dataclass(frozen=True)
class TableDiff:
    name: str
    label: str
    incoming: int
    new: int
    unchanged: int
    changed: int
    action: str
    # A few examples, so the screen can show what changed rather than
    # only how much. Capped because this goes down a wire.
    examples: tuple = ()

    @property
    def existing(self) -> int:
        return self.unchanged + self.changed

    def as_dict(self) -> dict:
        return {
            "name": self.name, "label": self.label,
            "incoming": self.incoming, "new": self.new,
            "unchanged": self.unchanged, "changed": self.changed,
            "existing": self.existing, "action": self.action,
            "examples": list(self.examples),
        }


# Each table: how to key an incoming row, how to key and read a
# recorded one, and which fields decide "changed". `action` says what
# apply() will do with a row that already exists, so the preview
# promises exactly what the commit does.
def _specs(p: Plan):
    return (
        dict(name="branches", label="Branches", rows=p.branches,
             action="update",
             key=lambda r: (r["code"].upper(),),
             vals=lambda r: (r["name"], r["city"]),
             sql="select upper(code), name, city from public.locations"),
        dict(name="suppliers", label="Suppliers", rows=p.suppliers,
             action="update",
             key=lambda r: (supplier_code(r["name"]),),
             vals=lambda r: (r["name"],),
             sql="select code, name from public.suppliers"),
        dict(name="items", label="Items", rows=p.items,
             action="update",
             key=lambda r: (r["sku"],),
             vals=lambda r: (r["description"], r["category"], r["lead"],
                             r["lead_std"], bool(r["critical"])),
             sql="select sku, description, category, lead_time_days,"
                 " lead_time_days_std, is_clinical_critical"
                 " from public.items"),
        # The ledger is append-only, so a recorded opening movement can
        # only ever be skipped. There is no such thing as a changed one:
        # a different opening quantity is a new fact about stock, and
        # correcting it is a count, not an import.
        # Only the rows that become movements. A zero opening writes
        # no movement -- the ledger forbids it -- so counting it here
        # meant one out-of-stock line made every re-import of the same
        # file report a new row and never read as a replay.
        dict(name="stock", label="Opening stock",
             rows=[s for s in p.stock if s["qty"]],
             action="skip",
             key=lambda r: (f"import:opening:{r['sku']}:{r['branch']}",),
             vals=lambda r: (),
             sql="select client_ref from public.stock_movement"
                 " where client_ref is not null"),
        dict(name="history", label="Sales history", rows=p.history,
             action="update",
             key=lambda r: (r["sku"], r["branch"].upper(), r["period"]),
             vals=lambda r: (r["qty"],),
             sql="select i.sku, upper(l.code), d.period, d.quantity"
                 "  from public.demand_history d"
                 "  join public.items i on i.id = d.item_id"
                 "  join public.locations l on l.id = d.location_id"),
        dict(name="orders", label="On order", rows=p.orders,
             action="update",
             key=lambda r: (r["po"], r["sku"]),
             vals=lambda r: (r["qty"], r["price"]),
             sql="select po.po_number, i.sku, pl.qty, pl.unit_price"
                 "  from public.po_lines pl"
                 "  join public.purchase_orders po on po.id = pl.po_id"
                 "  join public.items i on i.id = pl.item_id"),
        # Receipts move stock and change a valuation, so a preview
        # that leaves them out is hiding the rows that matter most.
        # Skip rather than update: a receipt is an event, and the GRN
        # number is what says it is the same event.
        dict(name="receipts", label="Goods received", rows=p.receipts,
             action="skip",
             key=lambda r: (r["grn"], r["sku"]),
             vals=lambda r: (),
             sql="select g.grn_number, i.sku"
                 "  from public.grn_lines gl"
                 "  join public.goods_receipts g on g.id = gl.grn_id"
                 "  join public.items i on i.id = gl.item_id"),
        dict(name="fitment", label="Fitment", rows=p.fitment,
             action="skip",
             key=lambda r: (r["sku"], r["machine"]),
             vals=lambda r: (),
             sql="select i.sku, m.machine_key"
                 "  from public.item_machines im"
                 "  join public.items i on i.id = im.item_id"
                 "  join public.machines m on m.id = im.machine_id"),
        dict(name="customers", label="Customers", rows=p.customers,
             action="update",
             key=lambda r: (r["code"],),
             vals=lambda r: (r["name"], r["city"], bool(r["public"]),
                             r["terms"]),
             sql="select code, name, city, is_public_sector,"
                 " payment_terms_days from public.customers"),
        dict(name="invoices", label="Invoices", rows=p.invoices,
             action="update",
             key=lambda r: (r["no"],),
             vals=lambda r: (r["amount"], r["paid"], r["due"]),
             sql="select invoice_no, amount, amount_paid, due_date"
                 " from public.invoices"),
        dict(name="machines", label="Placed equipment", rows=p.machines,
             action="update",
             key=lambda r: (r["key"],),
             vals=lambda r: (r["model"], r["capital"], r["months"],
                             r["tests"], r["per_test"], r["margin"]),
             sql="select machine_key, model, capital_cost, contract_months,"
                 " expected_monthly_tests, revenue_per_test,"
                 " reagent_margin_pct from public.machines"),
        dict(name="accounts", label="Trial balance", rows=p.accounts,
             action="update",
             key=lambda r: (r["code"], r["to"]),
             vals=lambda r: (r["name"], r["line"], r["opening"],
                             r["closing"]),
             sql="select code, period_to, name, line, opening, closing"
                 "  from public.accounts"),
        dict(name="dividends", label="Dividends", rows=p.dividends,
             action="update",
             key=lambda r: (r["kind"], r["on"], r["amount"]),
             vals=lambda r: (r["note"],),
             sql="select kind, moved_on, amount, coalesce(note, '')"
                 "  from public.equity_movements"),
        dict(name="commitments", label="Committed payments",
             rows=p.commitments, action="update",
             key=lambda r: (r["label"], r["due"]),
             vals=lambda r: (r["kind"], r["amount"], bool(r["recurring"]),
                             r["every"] or 0),
             sql="select label, due_date, kind, amount, is_recurring,"
                 "       recurs_every_days from public.commitments"),
        dict(name="bank", label="Bank balance", rows=p.bank,
             action="update",
             key=lambda r: (r["as_at"], r["account"]),
             vals=lambda r: (r["balance"],),
             sql="select as_at, account_label, balance"
                 "  from public.cash_balances"),
        dict(name="employees", label="Employees", rows=p.employees,
             action="update",
             key=lambda r: (r["no"],),
             vals=lambda r: (r["name"], r["dept"], r["basic"], r["allow"],
                             r["medical"]),
             sql="select employee_no, name, department, basic_monthly,"
                 "       allowances_monthly, medical_monthly"
                 "  from public.employees"),
        dict(name="pipeline", label="Pipeline", rows=p.pipeline,
             action="update",
             key=lambda r: (r["ref"],),
             vals=lambda r: (r["stage"], r["value"], r["close"], r["qty"]),
             sql="select reference, stage, value, expected_close, quantity"
                 "  from public.opportunities"),
        dict(name="spend", label="Marketing spend", rows=p.spend,
             action="update",
             key=lambda r: (r["label"], r["on"]),
             vals=lambda r: (r["category"], r["amount"]),
             sql="select label, spent_on, category, amount"
                 "  from public.marketing_spend"),
        dict(name="contacts", label="Contacts", rows=p.contacts,
             action="update",
             key=lambda r: (r["name"],),
             vals=lambda r: (r["role"],),
             sql="select name, role_title from public.contacts"),
        dict(name="dealings", label="Dealings", rows=p.dealings,
             action="update",
             key=lambda r: (r["customer"], r["on"], r["summary"]),
             vals=lambda r: (r["channel"],),
             sql="select upper(c.code), x.happened_on, x.summary, x.channel"
                 "  from public.interactions x"
                 "  join public.customers c on c.id = x.customer_id"),
        dict(name="jobs", label="Service jobs", rows=p.jobs,
             action="update",
             key=lambda r: (r["no"],),
             vals=lambda r: (r["hours"], r["billed"], r["parts"],
                             r["charged"], bool(r["warranty"]),
                             bool(r["repeat"])),
             sql="select job_no, labour_hours, labour_hours_billed,"
                 " parts_cost, parts_charged, is_warranty, is_repeat_visit"
                 " from public.service_jobs"),
    )


MAX_EXAMPLES = 8


def inspect(conn, p: Plan) -> dict:
    """Compare the plan against what is recorded, table by table.

    Read-only, and it takes the same connection the commit will use, so
    what it reports is what that caller can actually see: under
    row-level security a table this role may not read comes back as
    nothing recorded, which is the truthful answer for them.

    A table whose query fails is reported as entirely new rather than
    raising. The preview is advice; being unable to give it must not
    stop somebody importing, and the commit's own conflict clauses are
    what actually prevent a duplicate.
    """
    run = conn.cursor() if hasattr(conn, "cursor") else conn

    tables, totals = [], {"new": 0, "unchanged": 0, "changed": 0}
    for spec in _specs(p):
        rows = spec["rows"]
        recorded: dict[tuple, tuple] = {}
        try:
            n_key = len(spec["key"](rows[0])) if rows else 1
            for got in run.execute(spec["sql"]).fetchall():
                recorded[_cmp(got[:n_key])] = _cmp(got[n_key:])
        except Exception:
            recorded = {}

        new = unchanged = changed = 0
        examples: list[dict] = []
        for r in rows:
            key = _cmp(spec["key"](r))
            if key not in recorded:
                new += 1
                continue
            if _cmp(spec["vals"](r)) == recorded[key]:
                unchanged += 1
                continue
            changed += 1
            if len(examples) < MAX_EXAMPLES:
                examples.append({
                    "key": " / ".join(str(k) for k in key),
                    "recorded": list(recorded[key]),
                    "incoming": list(_cmp(spec["vals"](r))),
                })

        tables.append(TableDiff(
            name=spec["name"], label=spec["label"], incoming=len(rows),
            new=new, unchanged=unchanged, changed=changed,
            action=spec["action"], examples=tuple(examples),
        ))
        totals["new"] += new
        totals["unchanged"] += unchanged
        totals["changed"] += changed

    # The sentence worth putting at the top of the screen. A full
    # replay is the common case -- somebody clicking twice, or
    # re-uploading to check -- and it deserves to be said plainly
    # rather than left for the person to work out from ten rows of
    # counts.
    if totals["new"] == 0 and totals["changed"] == 0:
        headline = ("Every row in this file is already recorded. "
                    "Committing it will change nothing.")
    elif totals["unchanged"] == 0 and totals["changed"] == 0:
        headline = "Nothing in this file is recorded yet."
    else:
        headline = (
            f"{totals['new']:,} new, {totals['changed']:,} changed and "
            f"{totals['unchanged']:,} already recorded. Committing "
            f"updates the changed rows and leaves the rest alone.")

    return {
        "headline": headline,
        "totals": totals,
        "is_replay": totals["new"] == 0 and totals["changed"] == 0,
        "tables": [t.as_dict() for t in tables],
    }
