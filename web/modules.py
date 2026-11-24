"""One runner per module, and the serialiser they all go through.

The registry in engine/platform.py says which modules exist and what
each one needs. This says what each one returns. Keeping the two apart
means the rail can be drawn, and a module's state decided, without
running any arithmetic -- which is what the first screen after sign-in
does.

Every runner takes the same `Facts` and returns a plain dict. No runner
reads the database directly except the four that have no engine module
behind them: freight, sell and customers keep records rather than
computing over them, and saying so in the registry is not enough -- the
reason there is no `engine/modules/freight.py` to call is that there is
no arithmetic, and inventing a module to hold a SELECT would suggest
otherwise.
"""

from __future__ import annotations

import dataclasses
import enum
from datetime import date, datetime
from typing import Any

from engine import report
from engine.platform import get as get_module
from engine.schema import StockStatus

# Facts about an item, which somebody may correct in place. Quantities
# are not here and never will be: on_hand comes from the ledger and
# unit_cost is derived from it, so typing over either would put a
# number on screen that no movement supports.
EDITABLE_ITEM_FIELDS = (
    "description", "category", "uom", "barcode", "lead_time_days",
    "lead_time_days_std", "is_clinical_critical", "list_price",
    "is_generic",
)


def jsonable(value: Any) -> Any:
    """Anything the engine returns, as something JSON can carry.

    The engine's results are frozen dataclasses holding enums, dates,
    tuples and other dataclasses. Converting them here rather than
    writing a dict literal per module means a field added to a result
    reaches the screen without a second edit, and -- more to the point
    -- cannot be silently dropped on the way.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # Rounded at the edge rather than in the arithmetic. Rounding
        # earlier accumulates, and a figure rounded twice is wrong by
        # more than a figure rounded once.
        return round(value, 4)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if dataclasses.is_dataclass(value):
        return {f.name: jsonable(getattr(value, f.name))
                for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        # Keys may be tuples -- the service-target table is keyed on
        # (abc, demand class) -- and JSON has no tuple keys.
        return {
            ("|".join(str(p) for p in k) if isinstance(k, tuple) else str(k)):
            jsonable(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    return str(value)


# ------------------------------------------------------------- the runners


def _blocked_donors(f) -> list[dict]:
    """Lines flagged as holding too much that can release nothing.

    EXCESS is a flat `excess_cover_months` of on-hand cover. The floor a
    branch keeps when donating is its order-up-to level, which is
    lead-time relative. On a long lead time the second sits above the
    first -- at 75 days order-up-to is roughly seven and a half months
    of demand against a six-month excess limit -- so a line can be
    marked as standing still and be correctly refused permission to
    move, both at once.

    Each half is defensible and the pair is contradictory, and
    reconciling them changes what EXCESS means on every stock screen.
    That is a decision for the business. What is not acceptable is the
    symptom: an empty transfer list, no error, and the most valuable
    output in the product appearing to do nothing. So they are counted
    and named here, with the two numbers that disagree, and the setting
    that resolves it.
    """
    out = []
    for r in f.stock:
        if r["status"] != StockStatus.EXCESS.value:
            continue
        floor = r.get("order_up_to")
        if floor is None or r["on_hand"] > floor:
            continue
        out.append({
            "item_key": r["item_key"],
            "description": r.get("description"),
            "location_id": r.get("location_id"),
            "on_hand": r["on_hand"],
            "months_cover": r.get("months_cover"),
            "order_up_to": floor,
            "excess_limit_months": f.thresholds.excess_cover_months,
            "lead_time_days": r.get("lead_time_days"),
        })
    return out


def _item_facts(f) -> dict:
    """Per-item facts the engine does not carry, keyed by SKU.

    Price and fitment are not inputs to any module -- no forecast or
    reorder point moves because of them -- so they do not belong on
    ItemSnapshot, which is the engine's contract and is deliberately
    the smallest thing the arithmetic needs. They are read here and
    merged into the rows the screen shows.
    """
    rows = {}
    for r in f.conn.execute(
        "select i.sku, i.list_price, i.is_generic, i.barcode, i.uom,"
        "       i.category,"
        "       coalesce((select string_agg(m.machine_key, ', '"
        "                         order by m.machine_key)"
        "                   from public.item_machines im"
        "                   join public.machines m on m.id = im.machine_id"
        "                  where im.item_id = i.id), '')"
        "  from public.items i where i.is_active"
    ).fetchall():
        rows[r[0]] = {
            "list_price": float(r[1]) if r[1] is not None else None,
            "is_generic": bool(r[2]),
            "barcode": r[3],
            "uom": r[4],
            "category": r[5],
            "fits": r[6] or "",
        }
    return rows


def _stock(f) -> dict:
    counts: dict[str, int] = {}
    for r in f.stock:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    blocked = _blocked_donors(f)

    # Margin per line, against the landed cost rather than the
    # supplier's invoice. That distinction is the whole product: the
    # same sum against an invoice price overstates margin by whatever
    # freight, duty and clearing added, which in this trade is a fifth
    # to a half.
    facts = _item_facts(f)
    for row in f.stock:
        extra = facts.get(row["item_key"], {})
        row["list_price"] = extra.get("list_price")
        row["is_generic"] = extra.get("is_generic", False)
        row["barcode"] = extra.get("barcode")
        row["fits"] = extra.get("fits") or (
            "Generic" if extra.get("is_generic") else "")
        price, cost = row["list_price"], row.get("unit_cost")
        row["margin"] = (round(price - cost, 2)
                         if price is not None and cost else None)
        row["margin_pct"] = (round((price - cost) / price, 4)
                             if price and cost and price > 0 else None)
        row["line_margin"] = (round((price - cost) * row["on_hand"], 2)
                              if price is not None and cost else None)
    return {
        "rows": f.stock,
        "transfers": f.transfers,
        "counts": counts,
        "blocked_donors": blocked,
        # Which columns a person may type into, decided here rather
        # than in the browser. Editing a fact about an item and
        # recording that forty units went out are different acts: the
        # first corrects something somebody wrote down, the second is
        # a ledger event that can never be edited, only reversed.
        # Blurring them is how a stock figure stops reconciling with
        # the movements behind it.
        "editable": list(EDITABLE_ITEM_FIELDS),
        # Two resolutions, and they do different things. Raising the
        # standing-still limit stops the line being flagged -- which is
        # the honest answer when the stock is held on purpose for a long
        # lead time. Lowering the cover target lowers the level the
        # branch has to keep, which is what actually frees stock to
        # move. Both are settings the person already has, and saying
        # only "raise the limit" would send somebody to the one knob
        # that cannot produce the transfer they are looking for.
        "blocked_note": (
            f"{len(blocked)} line{'s' if len(blocked) != 1 else ''} "
            f"counted as standing still cannot be moved: the lead time "
            f"puts the level this branch has to keep above the "
            f"{f.thresholds.excess_cover_months:g}-month limit that "
            f"flagged it. Raise the standing-still limit to stop "
            f"flagging stock held on purpose, or lower the cover target "
            f"to free some of it to move. Both are in Settings."
        ) if blocked else None,
        "totals": {
            "stock_value": round(sum(r["stock_value"] for r in f.stock), 2),
            "excess_value": round(sum(r["excess_value"] for r in f.stock), 2),
            "order_value": round(sum(r["order_value"] for r in f.stock), 2),
            "imports_avoided": round(
                sum(t["import_avoided"] for t in f.transfers), 2),
            "lines": len(f.stock),
            "branches": len(f.blocks),
        },
    }


def _buy(f) -> dict:
    s = f.sequencing
    return {
        "rows": report._order_rows(s),
        "cash_known": f.cash is not None,
        # Headroom belongs to the cash view, not to the sequencing:
        # module 8 computes what the weeks can fund and module 9 packs
        # into it. Reading it off the sequencing result would be asking
        # the packer how big the box was.
        "headroom": f.cash.headroom_for_orders if f.cash else None,
        "reason": getattr(s, "reason", None) if s else None,
        "totals": {
            "requested": s.value_requested if s else 0,
            "place_now": s.value_placed_now if s else 0,
            "scheduled": s.value_scheduled if s else 0,
            "unfunded": s.value_unfunded if s else 0,
            "by_transfer": s.value_covered_by_transfer if s else 0,
            "clinical_unfunded": s.clinical_unfunded if s else 0,
        } if s else {},
        "engine_caveat": getattr(s, "caveat", None) if s else None,
        # Said plainly because the ranking is otherwise unexplainable:
        # without a bank balance the headroom is unknown, so every
        # order reads as fundable and the order of the list is the only
        # thing that means anything.
        "caveat": None if f.opening_balance is not None else
        "No bank balance entered, so nothing here is checked against what "
        "you can actually fund -- only the order of priority is meaningful.",
    }


def _freight(f) -> dict:
    """Shipments and what landed on top of the supplier invoice.

    Read from `shipment_totals` and `landed_cost`, which migration
    0004 already built, rather than summing the cost rows here. Those
    views spread each cost on its own basis -- a shipping line bills by
    volume and customs by value -- and a screen that summed the costs
    instead would show the right total against the wrong lines, which
    is the error the allocation exists to prevent.
    """
    rows = []
    for r in f.conn.execute(
        "select s.reference, s.mode, s.etd, s.eta, s.arrived_at, s.status,"
        "       sup.name,"
        "       coalesce(t.fob_value, 0), coalesce(t.weight_kg, 0),"
        "       coalesce(t.volume_cbm, 0),"
        "       coalesce((select sum(c.amount * c.fx_rate)"
        "                   from public.shipment_costs c"
        "                  where c.shipment_id = s.id), 0),"
        "       (select count(*) from public.shipment_lines l"
        "          where l.shipment_id = s.id),"
        "       s.id,"
        # What actually reached an item. A charge spread by volume
        # across lines with no volume recorded divides by zero, so the
        # allocation is null and the cost reaches nothing -- the bill
        # sits in the shipment total and no price moves. The database
        # is right to refuse the division; what was missing is anybody
        # saying so.
        "       coalesce((select sum(lc.allocated_cost)"
        "                   from public.landed_cost lc"
        "                   join public.shipment_lines sl"
        "                     on sl.grn_line_id = lc.grn_line_id"
        "                  where sl.shipment_id = s.id), 0)"
        "  from public.shipments s"
        "  left join public.suppliers sup on sup.id = s.supplier_id"
        "  left join public.shipment_totals t on t.shipment_id = s.id"
        " order by coalesce(s.eta, s.etd) desc nulls last"
    ).fetchall():
        goods, costs = float(r[7] or 0), float(r[10] or 0)
        rows.append({
            "reference": r[0], "mode": r[1],
            "etd": r[2].isoformat() if r[2] else None,
            "eta": r[3].isoformat() if r[3] else None,
            "arrived_at": r[4].isoformat() if r[4] else None,
            "status": r[5], "supplier": r[6],
            "goods_value": round(goods, 2),
            "weight_kg": round(float(r[8] or 0), 3),
            "volume_cbm": round(float(r[9] or 0), 3),
            "landed_costs": round(costs, 2),
            # The figure the business is usually wrong about. Pricing
            # off the supplier invoice is wrong by this much on every
            # line.
            "uplift_pct": round(costs / goods, 4) if goods else None,
            "lines": int(r[11] or 0),
            "id": str(r[12]),
            "allocated": round(float(r[13] or 0), 2),
            "unallocated": round(costs - float(r[13] or 0), 2),
        })

    breakdown = [
        {"kind": r[0], "allocate_by": r[1],
         "amount": round(float(r[2]), 2)}
        for r in f.conn.execute(
            "select kind, allocate_by, sum(amount * fx_rate)"
            "  from public.shipment_costs"
            " group by kind, allocate_by order by 3 desc"
        ).fetchall()
    ]

    # What each received line actually ended up costing per unit. This
    # is the number that should be the unit cost everywhere else, and
    # the gap between it and the invoice price is the whole point.
    lines = [
        {
            "sku": r[0], "description": r[1],
            "shipment": r[2],
            "qty": round(float(r[3] or 0), 4),
            "fob_value": round(float(r[4] or 0), 2),
            "allocated_cost": round(float(r[5] or 0), 2),
            "landed_unit_cost": round(float(r[6] or 0), 4),
            "fob_unit_cost": round(
                float(r[4] or 0) / float(r[3]), 4) if r[3] else None,
            "batch": r[7],
            "expiry_date": r[8].isoformat() if r[8] else None,
        }
        for r in f.conn.execute(
            "select i.sku, i.description, s.reference, lc.qty, lc.fob_value,"
            "       lc.allocated_cost, lc.landed_unit_cost,"
            "       gl.batch, gl.expiry_date"
            "  from public.landed_cost lc"
            "  join public.items i on i.id = lc.item_id"
            "  join public.grn_lines gl on gl.id = lc.grn_line_id"
            "  join public.shipment_lines sl on sl.grn_line_id = gl.id"
            "  join public.shipments s on s.id = sl.shipment_id"
            " order by lc.allocated_cost desc"
        ).fetchall()
    ]

    goods_total = sum(r["goods_value"] for r in rows)
    cost_total = sum(r["landed_costs"] for r in rows)

    # The headline uplift is measured only over shipments whose goods
    # are recorded. A shipment still at sea, or one whose receipt has
    # not been entered, contributes a cost and no denominator -- and
    # with three of five in that state the screen read 80.3% on top of
    # the invoice, which is not this business's uplift and is not
    # anybody's.
    #
    # The excluded ones are counted rather than hidden, because the
    # reason the figure moved is itself the thing worth knowing: it
    # means freight is sitting against goods nobody has booked in.
    # A charge that reached no item at all. Separate from a shipment
    # with no goods: these have goods, and the charge still went
    # nowhere, which is the harder one to notice because the screen
    # looks complete.
    stranded = [r for r in rows
                if r["goods_value"] > 0 and r["unallocated"] > 0.01]

    landed = [r for r in rows if r["goods_value"] > 0]
    landed_goods = sum(r["goods_value"] for r in landed)
    landed_costs = sum(r["landed_costs"] for r in landed)
    waiting = len(rows) - len(landed)
    return {
        "rows": rows,
        "breakdown": breakdown,
        "lines": lines,
        "totals": {
            "goods_value": round(goods_total, 2),
            "landed_costs": round(cost_total, 2),
            # Over the shipments that have goods, not over all of them.
            "uplift_pct": round(landed_costs / landed_goods, 4)
            if landed_goods else None,
            "shipments": len(rows),
            "shipments_landed": len(landed),
            "awaiting_goods": waiting,
            "unallocated": round(sum(r["unallocated"] for r in stranded), 2),
        },
        "awaiting_note": (
            f"{waiting} of {len(rows)} shipments carry costs with no goods "
            f"recorded against them, so they are left out of the uplift "
            f"above. Either they are still at sea, or what arrived has "
            f"not been entered on the Goods received sheet \u2014 and "
            f"until it is, their freight and duty reach no item."
        ) if waiting else None,
        "unallocated_note": (
            f"{len(stranded)} shipment"
            f"{'s carry' if len(stranded) != 1 else ' carries'} charges "
            f"that reached no item: "
            f"{', '.join(r['reference'] for r in stranded[:4])}"
            f"{', and others' if len(stranded) > 4 else ''}. A charge "
            f"spread by weight or volume needs those figures on the "
            f"received lines; with none recorded there is nothing to "
            f"divide by, so the bill sits in the total above and no "
            f"unit cost moves. Put the weights and volumes on the "
            f"Goods received sheet, or spread that charge by value or "
            f"quantity instead."
        ) if stranded else None,
    }


def _demand(f) -> dict:
    rows = f.forecasts
    return {
        "rows": rows,
        "totals": {
            "lines": len(rows),
            # Measured on a holdout, so an item with too little history
            # has no accuracy figure rather than a flattering one
            # computed on the data it was fitted to.
            "measured": len([r for r in rows if r["mape"] is not None]),
            "seasonal": len([r for r in rows if r["seasonality_fitted"]]),
        },
    }


def _cash(f) -> dict:
    c = f.cash
    return {
        "rows": report._cash_rows(c),
        "opening_balance": f.opening_balance,
        "balance_as_at": f.balance_as_at.isoformat()
        if f.balance_as_at else None,
        "totals": {
            "lowest_balance": c.lowest_balance,
            "lowest_week": c.lowest_week_index + 1,
            "weeks_negative": c.weeks_negative,
            "headroom": c.headroom_for_orders,
            "receipts": c.total_receipts,
            "payments": c.total_payments,
            "closing": c.closing_balance,
        } if c else {},
        "caveats": list(c.caveats) if c and hasattr(c, "caveats") else [],
        "needs_balance": f.opening_balance is None,
    }


def _money(f) -> dict:
    revenue, basis = f.monthly_revenue
    r = f.receivables
    return {
        "rows": report._customer_rows(r) if r else [],
        "revenue_basis": basis,
        "monthly_revenue": revenue,
        "buckets": jsonable(r.buckets) if r else [],
        "totals": {
            "outstanding": r.outstanding,
            "overdue": r.overdue,
            "dso_days": r.dso_days,
            "dso_gap_days": r.dso_gap_days,
            "financing_cost": r.financing_cost_annual,
            "cash_released_at_target": r.cash_released_at_target,
            "intercompany_excluded": r.intercompany_excluded,
            "public_sector_share": r.public_sector_share,
        } if r else {},
    }


def _sell(f) -> dict:
    """Invoices raised, and what was kept on them.

    Margin is left out rather than estimated. It needs the cost of what
    went out on the invoice, and invoice lines do not exist yet -- an
    invoice here is a header with an amount. A margin column filled by
    apportioning the total across whatever was sold that month would be
    a plausible number with nothing behind it, and this is the figure
    people would make pricing decisions on.
    """
    rows = [
        {
            "invoice_no": r[0], "customer": r[1],
            "invoice_date": r[2].isoformat() if r[2] else None,
            "due_date": r[3].isoformat() if r[3] else None,
            "amount": round(float(r[4] or 0), 2),
            "amount_paid": round(float(r[5] or 0), 2),
            "outstanding": round(float(r[4] or 0) - float(r[5] or 0), 2),
            "currency": r[6],
            "id": str(r[7]),
        }
        for r in f.conn.execute(
            "select i.invoice_no, c.name, i.invoice_date, i.due_date,"
            "       i.amount, i.amount_paid, i.currency, i.id"
            "  from public.invoices i"
            "  join public.customers c on c.id = i.customer_id"
            " order by i.invoice_date desc"
        ).fetchall()
    ]
    billed = sum(r["amount"] for r in rows)
    return {
        "rows": rows,
        "totals": {
            "billed": round(billed, 2),
            "collected": round(sum(r["amount_paid"] for r in rows), 2),
            "outstanding": round(sum(r["outstanding"] for r in rows), 2),
            "invoices": len(rows),
        },
        "note": "Margin per invoice needs invoice lines, which are not "
                "recorded yet. Nothing here is estimated.",
    }


def _books(f) -> dict:
    """The statements, and the three checks that have to tie.

    The checks are the output, not a footnote. A cash flow that does
    not tie is not a presentational problem -- it means a
    working-capital movement has been missed -- so the screen leads
    with whether the books hold together.
    """
    from engine.db import snapshot
    from engine.modules.finance import evaluate

    if not f.accounts:
        return {"statements": None, "checks": [], "period": None}
    period_from, period_to = snapshot.accounts_period(f.conn, f.as_of)
    dividends = snapshot.read_dividends(
        f.conn, period_from or f.as_of, period_to or f.as_of)
    s = evaluate(f.accounts, f.thresholds,
                 period_from=period_from, period_to=period_to,
                 dividends=dividends)
    return {
        "statements": jsonable(s),
        "checks": jsonable(getattr(s, "checks", ())),
        "dividends": dividends,
        "period": {
            "from": period_from.isoformat() if period_from else None,
            "to": period_to.isoformat() if period_to else None,
        },
        "accounts": len(f.accounts),
    }


def _customers(f) -> dict:
    customers = [
        {
            "id": str(r[0]), "code": r[1], "name": r[2], "city": r[3],
            "is_public_sector": bool(r[4]), "is_intercompany": bool(r[5]),
            "credit_limit": float(r[6]) if r[6] is not None else None,
            "payment_terms_days": int(r[7] or 0),
            "outstanding": round(float(r[8] or 0), 2),
            # The point of a credit limit is being told before the next
            # order, not after. Computed here because it needs the
            # ageing and the limit together, and neither table has both.
            "over_limit": (r[6] is not None
                           and float(r[8] or 0) > float(r[6])),
            "contacts": int(r[9] or 0),
            "last_contact": r[10].isoformat() if r[10] else None,
            "is_active": bool(r[11]),
        }
        for r in f.conn.execute(
            "select c.id, c.code, c.name, c.city, c.is_public_sector,"
            "       c.is_intercompany, c.credit_limit, c.payment_terms_days,"
            "       (select coalesce(sum(i.amount - i.amount_paid), 0)"
            "          from public.invoices i where i.customer_id = c.id),"
            "       (select count(*) from public.contacts ct"
            "          where ct.customer_id = c.id),"
            "       (select max(x.happened_on) from public.interactions x"
            "          where x.customer_id = c.id),"
            "       c.is_active"
            "  from public.customers c order by c.name"
        ).fetchall()
    ]
    suppliers = [
        {"id": str(r[0]), "code": r[1], "name": r[2],
         "contacts": int(r[3] or 0)}
        for r in f.conn.execute(
            "select s.id, s.code, s.name,"
            "       (select count(*) from public.contacts ct"
            "          where ct.supplier_id = s.id)"
            "  from public.suppliers s order by s.name"
        ).fetchall()
    ]
    return {
        "rows": customers,
        "suppliers": suppliers,
        "totals": {
            "customers": len(customers),
            "suppliers": len(suppliers),
            "over_limit": len([c for c in customers if c["over_limit"]]),
            "outstanding": round(
                sum(c["outstanding"] for c in customers), 2),
        },
    }


def _people(f) -> dict:
    """Payroll, at the rates in settings.

    engine/payroll.py refuses a date it has no slab table for rather
    than computing on last year's. That refusal is reported as itself:
    a payroll screen that silently used stale slabs would understate
    every deduction and nobody would find out until a filing.
    """
    from engine.payroll import evaluate

    if not f.employees:
        return {"slips": [], "totals": {}, "error": None}
    try:
        r = evaluate(f.employees, report._statutory(f.thresholds), f.as_of)
    except Exception as exc:
        return {"slips": [], "totals": {}, "error": str(exc)}
    full = jsonable(r)
    slips = full.pop("slips", [])
    return {
        "slips": slips,
        # Which slab table produced these figures, and where it came
        # from. Shown rather than held, because Pakistani slabs change
        # with each Finance Act: a payroll figure whose source is not
        # on the screen is a number nobody can check against the
        # notification it supposedly came from.
        "table": {"name": full.get("table_name"),
                  "source": full.get("table_source")},
        "totals": full,
        "error": None,
    }


def _equipment(f) -> dict:
    rows = f.machines
    capital = sum(r["capital_cost"] for r in rows)
    recovered = sum(r["net_recovered"] for r in rows)
    return {
        "rows": rows,
        "totals": {
            "placements": len(rows),
            "capital_deployed": round(capital, 2),
            "capital_recovered": round(recovered, 2),
            "recovery_pct": round(recovered / capital, 4)
            if capital > 0 else None,
            "downtime_margin_lost": round(
                sum(r["revenue_lost_to_downtime"] for r in rows), 2),
        },
    }


def _service(f) -> dict:
    r = f.service
    return {
        "rows": jsonable(getattr(r, "by_machine", ())) if r else [],
        "totals": {
            "value_lost": r.total_value_lost,
            "labour_lost": getattr(r, "labour_value_lost", None),
            "parts_lost": getattr(r, "parts_value_lost", None),
            "recovery_rate": r.recovery_rate,
            "repeat_visit_rate": r.repeat_visit_rate,
            "downtime_days": r.downtime_days,
            "jobs": len(f.jobs),
        } if r else {},
    }


def _intelligence(f) -> dict:
    """The pipeline, checked against the warehouse and the bank.

    That check is the reason this is here rather than in a CRM. An
    opportunity closing in six weeks for a line with eleven weeks of
    lead time and none on the shelf is not a forecast; it is a
    disappointment with a date on it, and only a product that holds the
    stock position can say so.
    """
    from engine.modules.marketing import evaluate

    r = evaluate(
        f.opportunities, f.marketing_spend, f.stock,
        cash_headroom=(f.cash.headroom_for_orders if f.cash else None),
        thresholds=f.thresholds, as_of=f.as_of,
    ) if f.opportunities else None
    if r is None:
        return {"rows": [], "totals": {}}
    full = jsonable(r)
    rows = full.pop("pipeline", []) or full.pop("rows", [])
    # at_risk_supply is a list of opportunities, not a figure, and the
    # tile beside it was formatting the list as money -- which is how
    # "Cannot supply: NaN" reached the screen. The useful number is
    # what those opportunities are worth, which is the thing somebody
    # would act on: pipeline that will be lost for want of stock
    # rather than for want of a better price.
    at_risk = full.get("at_risk_supply") or []
    full["at_risk_supply_value"] = round(
        sum(float(a.get("value") or 0) for a in at_risk), 2)
    full["at_risk_supply_count"] = len(at_risk)
    return {"rows": rows, "totals": full,
            "cash_known": f.cash is not None}


def _quality(f) -> dict:
    """What is wrong with the figures, before anybody acts on them.

    engine/sanity.py has run on every import since the beginning and
    its findings reached nobody: they went into the command-line
    report and stopped there. That is the wrong half to hide. A
    confidently wrong number is worse than a missing one, and the
    checks that catch it -- a unit cost out by a hundred, two spellings
    of one customer, a receipt dated before its order -- are the
    difference between a figure somebody can act on and one they
    cannot.

    Every finding carries what it does to the numbers and how to fix
    it, because a data-quality report that only lists faults is a list
    of homework.
    """
    from engine import sanity

    findings = f.findings
    return {
        "rows": jsonable(findings),
        "totals": sanity.summary(findings),
        # Said rather than left to be read off three counts. "Nothing
        # known wrong" is deliberately not "right": these checks find
        # what they look for, and silence is not proof.
        "verdict": (
            "Nothing here is known to be wrong. That is not the same as "
            "right: these checks find what they look for."
            if not findings else
            f"{len(findings)} thing{'s' if len(findings) != 1 else ''} "
            f"worth looking at before you act on these figures."
        ),
    }


RUNNERS = {
    "quality": _quality,
    "stock": _stock,
    "buy": _buy,
    "freight": _freight,
    "demand": _demand,
    "cash": _cash,
    "money": _money,
    "sell": _sell,
    "books": _books,
    "customers": _customers,
    "people": _people,
    "equipment": _equipment,
    "service": _service,
    "intelligence": _intelligence,
}


def run(module_id: str, facts) -> dict:
    """One module's payload, with the state that explains an empty one.

    `missing` is why there is nothing to show: the record types the
    module needs and this organisation has none of. A screen that said
    only "no data" would leave the person guessing which of thirteen
    tables to go and fill.
    """
    module = get_module(module_id)
    runner = RUNNERS[module_id]
    present = facts.present
    missing = module.missing(present)
    payload = {
        "module": module_id,
        "title": module.title,
        "missing": list(missing),
        "refused": sorted(facts.refused),
        "as_of": facts.as_of.isoformat(),
    }
    if missing:
        payload["data"] = None
        return payload
    payload["data"] = jsonable(runner(facts))
    # Collected after the run, because a refusal happens when the
    # runner reads, not when the module is looked up.
    payload["refused"] = sorted(facts.refused)
    return payload


# ----------------------------------------------------------- the headline
#
# What a module says about itself in one figure, for the home screen.
#
# Kept here, beside the runners, rather than in the browser. A headline
# is a claim about what the module is for -- that Ordering is about what
# can be funded rather than what policy asked for, that Service is about
# margin lost rather than jobs done -- and that claim belongs with the
# arithmetic it reads, not with the markup that prints it.
#
# `second` is the figure that stops the first being read wrongly. Stock
# worth a crore is good news until you see how much of it is standing
# still; a pipeline of fifty million means nothing without the win rate
# behind it.


def _n(d: dict, *path, default=None):
    for k in path:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d if d is not None else default


HEADLINES: dict[str, Any] = {
    "stock": lambda d: {
        "value": _n(d, "totals", "stock_value"), "kind": "money",
        "label": "on the shelf",
        "second": _n(d, "totals", "excess_value"),
        "second_label": "standing still", "second_kind": "money",
    },
    "buy": lambda d: {
        "value": _n(d, "totals", "place_now"), "kind": "money",
        "label": "to place now",
        "second": _n(d, "totals", "unfunded"),
        "second_label": "policy wants, cash will not fund",
        "second_kind": "money",
    },
    "freight": lambda d: {
        "value": _n(d, "totals", "uplift_pct"), "kind": "pct",
        "label": "on top of the invoice",
        "second": _n(d, "totals", "landed_costs"),
        "second_label": "freight, duty and clearing", "second_kind": "money",
    },
    "demand": lambda d: {
        "value": _n(d, "totals", "measured"), "kind": "count",
        "label": "lines with a forecast",
        "second": _n(d, "totals", "lines"),
        "second_label": "lines in all", "second_kind": "count",
    },
    "cash": lambda d: {
        "value": _n(d, "totals", "lowest_balance"), "kind": "money",
        "label": "lowest point in thirteen weeks",
        "second": _n(d, "totals", "weeks_negative"),
        "second_label": "weeks under", "second_kind": "count",
    },
    "money": lambda d: {
        "value": _n(d, "totals", "outstanding"), "kind": "money",
        "label": "owed to you",
        "second": _n(d, "totals", "overdue"),
        "second_label": "overdue", "second_kind": "money",
    },
    "sell": lambda d: {
        "value": _n(d, "totals", "billed"), "kind": "money",
        "label": "billed",
        "second": _n(d, "totals", "collected"),
        "second_label": "collected", "second_kind": "money",
    },
    "books": lambda d: {
        # The books have no single figure, and inventing one would be
        # the wrong answer: what somebody wants to know is whether they
        # tie.
        "value": sum(1 for c in (d.get("checks") or []) if _tied(c)),
        "kind": "count", "label": "checks that tie",
        "second": len(d.get("checks") or []),
        "second_label": "checks in all", "second_kind": "count",
    },
    "customers": lambda d: {
        "value": _n(d, "totals", "customers"), "kind": "count",
        "label": "customers",
        "second": _n(d, "totals", "over_limit"),
        "second_label": "over their limit", "second_kind": "count",
    },
    "people": lambda d: {
        "value": _n(d, "totals", "employer_cost_monthly"), "kind": "money",
        "label": "a month, all in",
        "second": _n(d, "totals", "headcount"),
        "second_label": "on the payroll", "second_kind": "count",
    },
    "equipment": lambda d: {
        "value": _n(d, "totals", "capital_deployed"), "kind": "money",
        "label": "capital placed with customers",
        "second": _n(d, "totals", "recovery_pct"),
        "second_label": "recovered so far", "second_kind": "pct",
    },
    "service": lambda d: {
        "value": _n(d, "totals", "value_lost"), "kind": "money",
        "label": "margin lost to service",
        "second": _n(d, "totals", "jobs"),
        "second_label": "jobs", "second_kind": "count",
    },
    "quality": lambda d: {
        "value": _n(d, "totals", "total"), "kind": "count",
        "label": "things to check",
        "second": _n(d, "totals", "blocks"),
        "second_label": "that stop an answer", "second_kind": "count",
    },
    "intelligence": lambda d: {
        "value": _n(d, "totals", "weighted_value"), "kind": "money",
        "label": "pipeline, weighted",
        "second": _n(d, "totals", "open_count"),
        "second_label": "open", "second_kind": "count",
    },
}


def _tied(check) -> bool:
    """A finance check that holds.

    The checks arrive as whatever jsonable() made of them, which is a
    dict for a dataclass and an object for anything else, so this reads
    both rather than assuming one.
    """
    if isinstance(check, dict):
        for k in ("ok", "tied", "passed", "holds"):
            if k in check:
                return bool(check[k])
        return False
    return bool(getattr(check, "ok", False))


def headline(module_id: str, data: dict | None) -> dict | None:
    """One figure for the home screen, or None if there is nothing yet."""
    if not data or module_id not in HEADLINES:
        return None
    try:
        h = HEADLINES[module_id](data)
    except Exception:
        return None
    if h.get("value") is None:
        return None
    return h


def insights(f) -> list[dict]:
    """The ranked cross-module list: everything worth a morning, in
    money order.

    engine/report.py has produced this since the beginning and it went
    into the command-line report and stopped there. It is the only
    thing in the engine that reads every module at once and says which
    of fourteen screens to open first, which makes it the single most
    useful output the product has and the one nobody could see.

    It is a list of judgements, not a list of figures, so it ranks: a
    blocking data fault comes above any amount of money, because
    acting on a confidently wrong number is worse than acting on none.
    """
    return report._rank_insights(
        f.stock, f.transfers, f.receivables, f.service, f.machines,
        f.cash, f.sequencing, f.thresholds, f.forecasts, f.findings,
    )
