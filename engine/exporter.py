"""Your data, back out, in the shape it came in.

Two reasons this exists, and the second is the one that matters.

A business about to put its stock, its cash position and its payroll
into somebody else's software asks, reasonably, how it gets them out
again. "Export to CSV" is a thin answer when the thing being exported
is twenty tables that reference each other. So the export is the
import workbook: the same twenty-four sheets, the same headers, the
same codes in the same columns.

Which gives the second reason. An export that can be imported is an
export that has been checked. Round-tripping a business's own data --
export, import, and nothing changes -- is a far stronger claim than
"the file opens", and it is a test rather than a promise.

It reuses the workbook writer rather than reimplementing the sheets.
A second writer would drift from the first the day somebody added a
column, and the drift would show up as a file that exports fine and
imports wrong.
"""

from __future__ import annotations

import datetime as _dt
from datetime import date

MONTHS = 24


def _months(as_of: date, n: int = MONTHS) -> list[str]:
    out, y, m = [], as_of.year, as_of.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def _day(v):
    if isinstance(v, _dt.datetime):
        return v.date().isoformat()
    if isinstance(v, _dt.date):
        return v.isoformat()
    return v or ""


def _f(v) -> float:
    return float(v) if v is not None else 0.0


def collect(conn, as_of: date | None = None) -> dict:
    """Everything this connection can see, in the writer's shapes.

    Read through the caller's own connection, so the export contains
    exactly what that person is allowed to see and not a row more. An
    export built as the table owner would be a quiet way around every
    policy in the schema.
    """
    run = conn.cursor() if hasattr(conn, "cursor") else conn
    as_of = as_of or date.today()
    months = _months(as_of)

    org = run.execute(
        "select name, coalesce(currency, 'PKR')"
        "  from public.organisations order by created_at limit 1"
    ).fetchone()
    spec = {
        "name": org[0] if org else "Your business",
        "what": "Exported from Landed on " + as_of.isoformat(),
        "currency": org[1] if org else "PKR",
    }

    branches = [
        (r[0], r[1], r[2] or "", bool(r[3]))
        for r in run.execute(
            "select code, name, city, is_warehouse from public.locations"
            " where is_active order by code").fetchall()
    ]
    suppliers = [
        (r[0], r[1] or "", r[2] or "")
        for r in run.execute(
            "select name, country, currency from public.suppliers"
            " order by name").fetchall()
    ]
    items = [
        {"sku": r[0], "description": r[1], "category": r[2] or "",
         "supplier": r[3] or "", "uom": r[4] or "each",
         "lead": _f(r[5]), "lead_std": _f(r[6]),
         "critical": "yes" if r[7] else "",
         "price": _f(r[8]) if r[8] is not None else None,
         "generic": "yes" if r[9] else ""}
        for r in run.execute(
            "select i.sku, i.description, i.category, s.name, i.uom,"
            "       i.lead_time_days, i.lead_time_days_std,"
            "       i.is_clinical_critical, i.list_price, i.is_generic"
            "  from public.items i"
            "  left join public.suppliers s on s.id = i.supplier_id"
            " where i.is_active order by i.sku").fetchall()
    ]

    # On hand from the ledger and cost from the valuation view, which
    # is the landed figure wherever the shipment costs are known. An
    # export that gave back the invoice price would quietly undo the
    # thing the product is for.
    stock = [
        {"sku": r[0], "branch": r[1], "qty": _f(r[2]), "cost": _f(r[3]),
         "expiry": _day(r[4])}
        for r in run.execute(
            "select i.sku, l.code, soh.on_hand,"
            "       coalesce(ic.avg_unit_cost, 0), il.expiry_date"
            "  from public.stock_on_hand soh"
            "  join public.items i on i.id = soh.item_id"
            "  join public.locations l on l.id = soh.location_id"
            "  left join public.item_cost ic on ic.item_id = soh.item_id"
            "  left join public.item_locations il"
            "    on il.item_id = soh.item_id"
            "   and il.location_id = soh.location_id"
            " where soh.on_hand <> 0 order by i.sku, l.code").fetchall()
    ]

    # The wide sheet: one row per item per branch, one column per month.
    seen: dict[tuple, dict] = {}
    for sku, branch, period, qty in run.execute(
        "select i.sku, l.code, d.period, sum(d.quantity)"
        "  from public.demand_monthly d"
        "  join public.items i on i.id = d.item_id"
        "  join public.locations l on l.id = d.location_id"
        " where d.period = any(%s)"
        " group by i.sku, l.code, d.period", (months,)
    ).fetchall():
        row = seen.setdefault(
            (sku, branch),
            {"sku": sku, "branch": branch, "series": [0.0] * len(months)})
        row["series"][months.index(period)] = _f(qty)
    history = [seen[k] for k in sorted(seen)]

    orders = [
        {"po": r[0], "sku": r[1], "branch": r[2], "supplier": r[3] or "",
         "qty": _f(r[4]), "price": _f(r[5]), "currency": r[6] or "PKR",
         "fx": _f(r[7]) or 1, "ordered": _day(r[8]),
         "expected": _day(r[9]), "status": r[10]}
        for r in run.execute(
            "select po.po_number, i.sku, l.code, s.name, pl.qty,"
            "       pl.unit_price, po.currency, po.fx_rate,"
            "       po.ordered_at, po.expected_at, po.status"
            "  from public.po_lines pl"
            "  join public.purchase_orders po on po.id = pl.po_id"
            "  join public.items i on i.id = pl.item_id"
            "  join public.locations l on l.id = po.location_id"
            "  left join public.suppliers s on s.id = po.supplier_id"
            " order by po.po_number, i.sku").fetchall()
    ]

    # One row per cost line, which is how the sheet is read back.
    shipments = [
        {"ref": r[0], "po": r[1] or "", "mode": r[2] or "",
         "etd": _day(r[3]), "eta": _day(r[4]),
         "cost_type": r[5] or r[6], "amount": _f(r[7]),
         "allocate_by": r[8]}
        for r in run.execute(
            "select s.reference,"
            "       (select po.po_number from public.purchase_orders po"
            "         join public.goods_receipts g on g.po_id = po.id"
            "         join public.grn_lines gl on gl.grn_id = g.id"
            "         join public.shipment_lines sl2"
            "           on sl2.grn_line_id = gl.id"
            "        where sl2.shipment_id = s.id limit 1),"
            "       s.mode, s.etd, s.eta, c.note, c.kind, c.amount,"
            "       c.allocate_by"
            "  from public.shipment_costs c"
            "  join public.shipments s on s.id = c.shipment_id"
            " order by s.reference, c.kind").fetchall()
    ]

    customers = [
        {"code": r[0], "name": r[1], "city": r[2] or "",
         "public": bool(r[3]), "inter": bool(r[4]), "terms": int(r[5] or 0),
         "contact": r[6] or "", "email": r[7] or ""}
        for r in run.execute(
            "select code, name, city, is_public_sector, is_intercompany,"
            "       payment_terms_days, contact_name, contact_email"
            "  from public.customers order by code").fetchall()
    ]
    invoices = [
        {"no": r[0], "customer": r[1], "branch": r[2] or "",
         "date": _day(r[3]), "due": _day(r[4]),
         "amount": _f(r[5]), "paid": _f(r[6])}
        for r in run.execute(
            "select i.invoice_no, c.code, l.code, i.invoice_date,"
            "       i.due_date, i.amount, i.amount_paid"
            "  from public.invoices i"
            "  join public.customers c on c.id = i.customer_id"
            "  left join public.locations l on l.id = i.location_id"
            " order by i.invoice_date").fetchall()
    ]
    machines = [
        {"key": r[0], "model": r[1], "serial": r[2] or "",
         "customer": r[3] or "", "branch": r[4] or "",
         "placed": _day(r[5]), "capital": _f(r[6]),
         "months": int(r[7] or 0), "tests": _f(r[8]),
         "per_test": _f(r[9]), "margin": _f(r[10])}
        for r in run.execute(
            "select m.machine_key, m.model, m.serial_no, c.code, l.code,"
            "       m.placed_date, m.capital_cost, m.contract_months,"
            "       m.expected_monthly_tests, m.revenue_per_test,"
            "       m.reagent_margin_pct"
            "  from public.machines m"
            "  left join public.customers c on c.id = m.customer_id"
            "  left join public.locations l on l.id = m.location_id"
            " order by m.machine_key").fetchall()
    ]
    jobs = [
        {"no": r[0], "machine": r[1] or "", "branch": r[2] or "",
         "opened": _day(r[3]), "closed": _day(r[4]), "hours": _f(r[5]),
         "billed": _f(r[6]), "parts": _f(r[7]), "charged": _f(r[8]),
         "warranty": bool(r[9]), "repeat": bool(r[10]),
         "down": _f(r[11])}
        for r in run.execute(
            "select j.job_no, m.machine_key, l.code, j.opened_date,"
            "       j.closed_date, j.labour_hours, j.labour_hours_billed,"
            "       j.parts_cost, j.parts_charged, j.is_warranty,"
            "       j.is_repeat_visit, j.downtime_days"
            "  from public.service_jobs j"
            "  left join public.machines m on m.id = j.machine_id"
            "  left join public.locations l on l.id = j.location_id"
            " order by j.opened_date").fetchall()
    ]

    period = run.execute(
        "select min(period_from), max(period_to) from public.accounts"
    ).fetchone()
    accounts = [
        (r[0], r[1], r[2], _f(r[3]), _f(r[4]))
        for r in run.execute(
            "select code, name, line, opening, closing"
            "  from public.accounts"
            " where period_to = (select max(period_to) from public.accounts)"
            " order by code").fetchall()
    ]
    extra = {
        # What was received, so the export carries the goods its own
        # shipments refer to. Without this the file it produces would
        # import back as four shipments allocating to nothing, which
        # is the state the import was in before the receipts sheet
        # existed.
        "receipts": [
            {"grn": r[0], "shipment": r[1] or "", "po": r[2] or "",
             "branch": r[3], "sku": r[4], "qty": _f(r[5]),
             "price": _f(r[6]), "currency": r[7] or "",
             "fx": _f(r[8]), "received": _day(r[9]),
             "weight": _f(r[10]) if r[10] is not None else None,
             "volume": _f(r[11]) if r[11] is not None else None,
             "batch": r[12] or "", "expiry": _day(r[13])}
            for r in run.execute(
                "select g.grn_number, sh.reference, po.po_number,"
                "       l.code, i.sku, gl.qty, gl.unit_price, gl.currency,"
                "       gl.fx_rate, g.received_at, sl.weight_kg,"
                "       sl.volume_cbm, gl.batch, gl.expiry_date"
                "  from public.grn_lines gl"
                "  join public.goods_receipts g on g.id = gl.grn_id"
                "  join public.items i on i.id = gl.item_id"
                "  join public.locations l on l.id = g.location_id"
                "  left join public.purchase_orders po on po.id = g.po_id"
                "  left join public.shipment_lines sl"
                "    on sl.grn_line_id = gl.id"
                "  left join public.shipments sh on sh.id = sl.shipment_id"
                " order by g.grn_number, i.sku").fetchall()],
        # Ordered by the pair rather than by insertion, so two exports
        # of the same business produce the same sheet and a diff
        # between them means something changed.
        "fitment": [
            {"sku": r[0], "machine": r[1]}
            for r in run.execute(
                "select i.sku, m.machine_key"
                "  from public.item_machines im"
                "  join public.items i on i.id = im.item_id"
                "  join public.machines m on m.id = im.machine_id"
                " order by i.sku, m.machine_key").fetchall()],
        "accounts": accounts,
        "period": (_day(period[0]) or f"{as_of.year}-01-01",
                   _day(period[1]) or as_of.isoformat()),
        "dividends": [
            {"kind": r[0], "on": _day(r[1]), "amount": _f(r[2]),
             "note": r[3] or ""}
            for r in run.execute(
                "select kind, moved_on, amount, note"
                "  from public.equity_movements order by moved_on"
            ).fetchall()],
        "commitments": [
            {"label": r[0], "kind": r[1], "due": _day(r[2]),
             "amount": _f(r[3]), "recurring": "yes" if r[4] else "",
             "every": int(r[5] or 0), "branch": r[6] or ""}
            for r in run.execute(
                "select c.label, c.kind, c.due_date, c.amount,"
                "       c.is_recurring, c.recurs_every_days, l.code"
                "  from public.commitments c"
                "  left join public.locations l on l.id = c.location_id"
                " where c.is_settled = false order by c.due_date"
            ).fetchall()],
        "bank": [
            {"as_at": _day(r[0]), "balance": _f(r[1]), "account": r[2]}
            for r in run.execute(
                "select as_at, balance, account_label"
                "  from public.cash_balances order by as_at"
            ).fetchall()],
        "employees": [
            {"no": r[0], "name": r[1], "dept": r[2] or "",
             "branch": r[3] or "", "basic": _f(r[4]), "allow": _f(r[5]),
             "medical": _f(r[6]), "pf_er": _f(r[7]), "pf_ee": _f(r[8]),
             "joined": _day(r[9])}
            for r in run.execute(
                "select e.employee_no, e.name, e.department, l.code,"
                "       e.basic_monthly, e.allowances_monthly,"
                "       e.medical_monthly, e.employer_pf_rate,"
                "       e.employee_pf_rate, e.joined"
                "  from public.employees e"
                "  left join public.locations l on l.id = e.location_id"
                " where e.is_active order by e.employee_no"
            ).fetchall()],
        "pipeline": [
            {"ref": r[0], "customer": r[1] or "", "stage": r[2],
             "value": _f(r[3]), "close": _day(r[4]), "sku": r[5] or "",
             "qty": _f(r[6]), "source": r[7] or "", "owner": r[8] or "",
             "opened": _day(r[9]), "closed": _day(r[10])}
            for r in run.execute(
                "select o.reference, c.code, o.stage, o.value,"
                "       o.expected_close, i.sku, o.quantity, o.source,"
                "       o.owner_name, o.opened, o.closed"
                "  from public.opportunities o"
                "  left join public.customers c on c.id = o.customer_id"
                "  left join public.items i on i.id = o.item_id"
                " order by o.reference").fetchall()],
        "spend": [
            {"label": r[0], "category": r[1], "on": _day(r[2]),
             "amount": _f(r[3])}
            for r in run.execute(
                "select label, category, spent_on, amount"
                "  from public.marketing_spend order by spent_on"
            ).fetchall()],
        "contacts": [
            {"customer": r[0] or "", "supplier": r[1] or "", "name": r[2],
             "role": r[3] or "", "email": r[4] or "", "phone": r[5] or "",
             "whatsapp": r[6] or "", "primary": "yes" if r[7] else ""}
            for r in run.execute(
                "select c.code, s.name, ct.name, ct.role_title, ct.email,"
                "       ct.phone, ct.whatsapp, ct.is_primary"
                "  from public.contacts ct"
                "  left join public.customers c on c.id = ct.customer_id"
                "  left join public.suppliers s on s.id = ct.supplier_id"
                " order by ct.name").fetchall()],
        "dealings": [
            {"customer": r[0], "on": _day(r[1]), "channel": r[2],
             "summary": r[3], "next": r[4] or "", "next_on": _day(r[5])}
            for r in run.execute(
                "select c.code, x.happened_on, x.channel, x.summary,"
                "       x.next_step, x.next_step_on"
                "  from public.interactions x"
                "  join public.customers c on c.id = x.customer_id"
                " order by x.happened_on desc").fetchall()],
    }

    return {
        "spec": spec, "branches": branches, "suppliers": suppliers,
        "items": items, "stock": stock, "history": history,
        "orders": orders, "shipments": shipments, "months": months,
        "customers": customers, "invoices": invoices,
        "machines": machines, "jobs": jobs, "extra": extra,
    }


def workbook(conn, as_of: date | None = None):
    """The same twenty-four sheets the import reads.

    Imported by its proper path rather than by putting tools/ on
    sys.path. The path trick worked here and would have failed in the
    image, where the working directory is not the repository root --
    and it read to the requirements check as an undeclared third-party
    package, which is the same check that once caught a deploy that
    built and then would not start.
    """
    from tools.make_import_workbook import render

    d = collect(conn, as_of)
    return render(
        d["spec"], d["branches"], d["suppliers"], d["items"], d["stock"],
        d["history"], d["orders"], d["shipments"], d["months"],
        d["customers"], d["invoices"], d["machines"], d["jobs"],
        d["extra"])


def to_bytes(conn, as_of: date | None = None) -> bytes:
    from io import BytesIO

    buf = BytesIO()
    workbook(conn, as_of).save(buf)
    return buf.getvalue()
