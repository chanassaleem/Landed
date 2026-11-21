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

REQUIRED_SHEETS = ("Branches", "Items", "Stock")
OPTIONAL_SHEETS = ("Sales history", "On order", "Shipments", "Suppliers")

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
    """
    if not p.ok:
        raise BadWorkbook("This plan has problems that must be fixed first.")

    # Every insert below reads `select id from public.organisations limit 1`
    # to find the caller's organisation. With no organisation that
    # returns no rows, the insert writes nothing, and the fetchone()
    # after it hands back None -- which surfaced to the first real user
    # as "TypeError" and nothing else. Checked once, here, and said in
    # words.
    row = conn.execute("select count(*) from public.organisations").fetchone()
    if not row or not row[0]:
        raise NoOrganisation(
            "This account has no organisation yet, so there is nowhere to "
            "put the import. Name your business first.")

    as_of = as_of or date.today()
    made = {k: 0 for k in
            ("branches", "suppliers", "items", "movements", "history",
             "orders", "shipments")}

    # Branches
    loc = {}
    for b in p.branches:
        row = conn.execute(
            "insert into public.locations (org_id, code, name, city, "
            "is_warehouse) select id, %s, %s, %s, %s from public.organisations "
            "limit 1 on conflict (org_id, code) do update set name = "
            "excluded.name returning id",
            (b["code"], b["name"], b["city"] or None, b["warehouse"]),
        ).fetchone()
        loc[b["code"]] = row[0]
        made["branches"] += 1

    # Suppliers, named on their own sheet or only on Items
    names = {s["name"] for s in p.suppliers}
    names |= {i["supplier"] for i in p.items if i["supplier"]}
    sup = {}
    for name in sorted(n for n in names if n):
        spec = next((s for s in p.suppliers if s["name"] == name), None)
        # Suppliers are unique on (org_id, code), not on name -- a
        # business can have two contacts at one company. The sheet asks
        # only for a name, so the code is derived from it and stays
        # stable across imports, which is what makes the upsert work.
        row = conn.execute(
            "insert into public.suppliers (org_id, code, name, country, "
            "currency) select id, %s, %s, %s, %s from public.organisations "
            "limit 1 on conflict (org_id, code) do update set "
            "name = excluded.name returning id",
            (supplier_code(name), name, (spec or {}).get("country") or None,
             (spec or {}).get("currency") or "PKR"),
        ).fetchone()
        sup[name] = row[0]
        made["suppliers"] += 1

    # Items
    item = {}
    for i in p.items:
        row = conn.execute(
            "insert into public.items (org_id, sku, description, category, "
            "uom, supplier_id, lead_time_days, lead_time_days_std, "
            "is_clinical_critical) select id, %s, %s, %s, %s, %s, %s, %s, %s "
            "from public.organisations limit 1 "
            "on conflict (org_id, sku) do update set "
            "description = excluded.description, category = excluded.category, "
            "lead_time_days = excluded.lead_time_days, "
            "lead_time_days_std = excluded.lead_time_days_std, "
            "is_clinical_critical = excluded.is_clinical_critical "
            "returning id",
            (i["sku"], i["description"], i["category"], i["uom"],
             sup.get(i["supplier"]), i["lead"], i["lead_std"], i["critical"]),
        ).fetchone()
        item[i["sku"]] = row[0]
        made["items"] += 1

    # Stock, as an opening movement rather than a balance. The ledger is
    # the only place a quantity lives; a balance written beside it is a
    # second number that can disagree with the first.
    for s in p.stock:
        iid, lid = item.get(s["sku"]), loc.get(s["branch"])
        if not iid or not lid:
            continue
        conn.execute(
            "insert into public.item_locations (org_id, item_id, location_id, "
            "expiry_date) select org_id, %s, %s, %s from public.items "
            "where id = %s on conflict (item_id, location_id) do update set "
            "expiry_date = excluded.expiry_date",
            (iid, lid, s["expiry"], iid))
        if s["qty"]:
            conn.execute(
                "insert into public.stock_movement (org_id, item_id, "
                "location_id, qty, unit_cost, reason, occurred_at, client_ref) "
                "select org_id, %s, %s, %s, %s, 'opening', %s, %s "
                "from public.items where id = %s",
                (iid, lid, s["qty"], s["cost"] or None, as_of,
                 f"import:opening:{s['sku']}:{s['branch']}", iid))
            made["movements"] += 1

    # History
    for hrow in p.history:
        iid, lid = item.get(hrow["sku"]), loc.get(hrow["branch"])
        if not iid or not lid or not PERIOD.match(hrow["period"]):
            continue
        conn.execute(
            "insert into public.demand_history (org_id, location_id, item_id, "
            "period, quantity, source) select org_id, %s, %s, %s, %s, 'import' "
            "from public.items where id = %s "
            "on conflict (org_id, location_id, item_id, period) "
            "do update set quantity = excluded.quantity",
            (lid, iid, hrow["period"], hrow["qty"], iid))
        made["history"] += 1

    # Purchase orders, header once and a line per row
    heads = {}
    for o in p.orders:
        iid, lid = item.get(o["sku"]), loc.get(o["branch"])
        if not iid or not lid:
            continue
        if o["po"] not in heads:
            row = conn.execute(
                "insert into public.purchase_orders (org_id, location_id, "
                "supplier_id, po_number, currency, fx_rate, status, "
                "ordered_at, expected_at) select org_id, %s, %s, %s, %s, %s, "
                "%s, %s, %s from public.items where id = %s "
                "on conflict (org_id, po_number) do update set "
                "status = excluded.status returning id",
                (lid, sup.get(o["supplier"]), o["po"], o["currency"],
                 o["fx"] or 1, o["status"], o["ordered"], o["expected"], iid),
            ).fetchone()
            heads[o["po"]] = row[0]
            made["orders"] += 1
        conn.execute(
            "insert into public.po_lines (org_id, po_id, item_id, qty, "
            "unit_price) select org_id, %s, %s, %s, %s from public.items "
            "where id = %s",
            (heads[o["po"]], iid, o["qty"], o["price"], iid))

    return made
