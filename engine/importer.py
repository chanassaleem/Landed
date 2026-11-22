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

PERIOD = re.compile(r"^\d{4}-\d{2}$")
MONTH_COL = re.compile(r"^sold[_ ](\d{4})[_-](\d{2})$", re.I)

TRUE = {"yes", "y", "true", "1", "t"}


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
                "customers": len(self.customers),
                "invoices": len(self.invoices),
                "machines": len(self.machines),
                "service_jobs": len(self.jobs),
            },
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


def _read_shipments(ws, p):
    h = _headers(ws)
    i = {name: n for n, name in enumerate(h)}
    for r, vals, _i in _rows(ws, h):
        ref = _text(vals[i.get("shipment_ref", 0)])
        if not ref:
            continue
        p.shipments.append({
            "ref": ref,
            "po": _text(vals[i["po_number"]]) if "po_number" in i else "",
            "mode": _text(vals[i["mode"]]) if "mode" in i else "",
            "etd": _day(vals[i["etd"]]) if "etd" in i else None,
            "eta": _day(vals[i["eta"]]) if "eta" in i else None,
            "cost_type": _text(vals[i["cost_type"]]) if "cost_type" in i else "",
            "amount": _num(vals[i["amount_pkr"]]) if "amount_pkr" in i else 0,
            "allocate_by": (_text(vals[i["allocate_by"]]).lower()
                            if "allocate_by" in i else "value") or "value",
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
            "is_clinical_critical) values (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (org_id, sku) do update set "
            "description = excluded.description, category = excluded.category, "
            "lead_time_days = excluded.lead_time_days, "
            "lead_time_days_std = excluded.lead_time_days_std, "
            "is_clinical_critical = excluded.is_clinical_critical "
            "returning id",
            (org, i["sku"], i["description"], i["category"], i["uom"],
             sup.get(i["supplier"]), i["lead"], i["lead_std"], i["critical"]),
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

    return made


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
        dict(name="stock", label="Opening stock", rows=p.stock,
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
