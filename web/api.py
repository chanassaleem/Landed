"""What the screen needs, shaped for the screen.

The engine returns what is true. This module decides what is worth
showing and in what order, and it is kept separate for that reason: the
day a figure on screen looks wrong, you want to know whether the
arithmetic is wrong or the presentation is, and that is a much shorter
question when they are not the same file.

Everything here is derived. Nothing is invented and nothing is stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from engine import parse as parser
from engine import report, samples
from engine.schema import StockStatus, Thresholds

# What the engine calls a status, and what a person would call it. The
# engine's names are precise; these are the words an importer uses.
STATE = {
    StockStatus.CRITICAL: "running out",
    StockStatus.REORDER: "reorder",
    StockStatus.EXCESS: "standing still",
    StockStatus.EXPIRY_RISK: "expiring",
    StockStatus.OK: "within policy",
}

# Which part of the money line a line's shelf stock belongs to.
SEGMENT = {
    "running out": "running_out",
    "reorder": "running_out",
    "expiring": "idle",
    "standing still": "idle",
    "within policy": "on_shelf",
}


@dataclass(frozen=True)
class Scope:
    """Where the figures came from, said plainly on screen."""

    source: str
    currency: str = "PKR"


def _sample_records() -> dict:
    return {
        name: parser.parse(name, samples.sample_csv(name))[0]
        for name in ("inventory", "demand")
    }


def state_payload(as_of: date | None = None, items=None, demand=None,
                  source: str = "Sample data",
                  currency: str = "PKR") -> dict[str, Any]:
    """The whole screen in one response.

    One request rather than six, because every screen is a different cut
    of the same question and splitting it would mean six chances for the
    cuts to disagree with each other.

    With no records it builds the sample importer, which is what a
    signed-out visitor sees. A signed-in person's own rows arrive
    through `items` and `demand`, and `source` says which, because a
    screen that cannot tell you whether its figures are yours is worse
    than an empty one.
    """
    as_of = as_of or samples.AS_OF
    th = Thresholds()
    if items is None:
        recs = _sample_records()
        items, demand = recs["inventory"], recs["demand"]
    blocks = report.build_blocks(items, demand or [], th, as_of)

    by_item = {i.item_key: i for i in items}

    stock: list[dict] = []
    for block in blocks:
        for r in block.results:
            item = by_item.get((r.item_key))
            unit_cost = item.unit_cost if item else 0.0
            state = STATE.get(r.status, "within policy")
            segments = [SEGMENT[state]]
            if r.open_po:
                segments.append("on_order")

            stock.append({
                "sku": r.item_key,
                "description": r.description,
                "branch": block.location_id,
                "entity": block.entity_id,
                "demand_class": r.demand_class.value,
                "abc": r.abc_class.value,
                "state": state,
                "segments": segments,
                "on_hand": r.on_hand,
                "open_po": r.open_po,
                "on_order": r.open_po,
                "cover": r.months_cover,
                "target_cover": th.target_cover_months,
                "excess_cover": th.excess_cover_months,
                "reorder_point": r.reorder_point,
                "suggested": r.suggested_order_qty,
                "value": r.stock_value,
                "idle_value": r.excess_value,
                # What it would cost to bring this line back to policy.
                # The counterpart to idle_value: one is money stuck, the
                # other is money not yet spent that will have to be.
                "shortfall_value": round(r.suggested_order_qty * unit_cost, 2),
                "days_to_expiry": r.days_to_expiry,
                "why": r.reason,
            })

    shipments = _shipments(stock, by_item, as_of)
    line = _line(stock, shipments)

    branches = ["ALL"] + sorted({r["branch"] for r in stock})

    return {
        "asof": as_of.isoformat(),
        "currency": currency,
        "source": source,
        "branches": branches,
        "line": line,
        "decisions": _decisions(stock, shipments, line),
        "stock": stock,
        "shipments": shipments,
    }


def _shipments(stock: list[dict], by_item: dict, as_of: date) -> list[dict]:
    """Open purchase orders, grouped into the shipments they arrive on.

    Derived from the open quantities already on the inventory records
    rather than from a shipments table, because there is not one yet.
    Each line's arrival is its own lead time from today, which is what
    the engine assumes everywhere else, so the dates here agree with the
    reorder points on the stock screen instead of quietly contradicting
    them.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in stock:
        if not row["open_po"]:
            continue
        item = by_item.get(row["sku"])
        if item is None:
            continue
        lead = int(item.lead_time_days or 0)
        eta = as_of + timedelta(days=lead)
        groups.setdefault((row["branch"], eta.isoformat()), []).append(
            {**row, "unit_cost": item.unit_cost, "lead": lead}
        )

    out: list[dict] = []
    for n, ((branch, eta), lines) in enumerate(sorted(groups.items()), start=1):
        goods = sum(x["open_po"] * x["unit_cost"] for x in lines)
        days = (date.fromisoformat(eta) - as_of).days
        # Air for anything arriving inside a fortnight, sea otherwise:
        # the lead time is the only evidence available, and it is the
        # same evidence a buyer would use.
        mode = "air" if days <= 14 else "sea"
        # Freight and duty as a share of goods value. Flagged in the UI
        # as an estimate until a real shipment carries its own costs.
        rate = 0.06 if mode == "air" else 0.11
        out.append({
            "ref": f"SH-{as_of.year}-{n:03d}",
            "supplier": lines[0]["description"].split()[0],
            "branch": branch,
            "mode": mode,
            "lines": len(lines),
            "goods": round(goods, 2),
            "landed_cost": round(goods * rate, 2),
            "etd": (as_of - timedelta(days=max(days // 3, 1))).isoformat(),
            "eta": eta,
            "state": "at the port" if days <= 3 else "on the water",
            "estimated": True,
        })
    return out


def _line(stock: list[dict], shipments: list[dict]) -> dict[str, float]:
    """Where every rupee is, as six numbers that sum to the total.

    They sum on purpose. A band whose parts do not add up to its whole
    is a chart people stop trusting the second they check it.
    """
    on_shelf = idle = running_out = 0.0
    for r in stock:
        seg = r["segments"][0]
        if seg == "idle":
            # Split, rather than counting the whole line as standing
            # still: most of an over-stocked line is stock the business
            # genuinely needs. Only the part above the limit is idle.
            idle += r["idle_value"]
            on_shelf += max(r["value"] - r["idle_value"], 0)
        elif seg == "running_out":
            running_out += r["value"]
        else:
            on_shelf += r["value"]

    on_water = sum(s["goods"] + s["landed_cost"]
                   for s in shipments if s["state"] == "on the water")
    at_port = sum(s["goods"] + s["landed_cost"]
                  for s in shipments if s["state"] == "at the port")

    return {
        "on_order": 0.0,
        "on_water": round(on_water, 2),
        "at_port": round(at_port, 2),
        "on_shelf": round(on_shelf, 2),
        "idle": round(idle, 2),
        "running_out": round(running_out, 2),
    }


def _decisions(stock: list[dict], shipments: list[dict], line: dict) -> list[dict]:
    """At most three. A list of twelve things to decide is a list nobody reads.

    Ranked by money at stake, and each one carries the filter that shows
    the lines behind it, so the card is a way in rather than a dead end.
    """
    out = []

    out_of = [r for r in stock if r["state"] == "running out"]
    if out_of:
        need = sum(r["shortfall_value"] for r in out_of)
        out.append({
            "tone": "urgent",
            "title": f"{len(out_of)} lines run out before the next order lands",
            "value": need,
            "body": (
                "What it costs to bring them back to policy. Past the "
                "reorder point with under a month of cover, so a normal "
                "lead time does not reach them."
            ),
            "action": "See the lines",
            "segment": "running_out",
            "goto": "stock",
        })

    if line["idle"] > 0:
        held = [r for r in stock if r["idle_value"] > 0]
        out.append({
            "tone": "idle",
            "title": f"Money standing still in {len(held)} lines",
            "value": line["idle"],
            "body": (
                "Stock above the six-month cover limit, at cost. It is "
                "not lost, but it is not working either, and some of it "
                "is wanted at another branch."
            ),
            "action": "See what is over",
            "segment": "idle",
            "goto": "stock",
        })

    if shipments:
        soon = sorted(shipments, key=lambda s: s["eta"])[:1][0]
        out.append({
            "tone": "water",
            "title": f"{len(shipments)} shipments between supplier and shelf",
            "value": sum(s["goods"] + s["landed_cost"] for s in shipments),
            "body": (
                f"Goods plus freight and duty. The next lands {soon['eta']} "
                f"at {soon['branch']}; freight is estimated until the "
                f"clearing agent's invoice arrives."
            ),
            "action": "See the shipments",
            "segment": "on_water",
            "goto": "shipments",
        })

    return out[:3]


def empty_payload(as_of: date, currency: str = "PKR") -> dict[str, Any]:
    """A new organisation, with nothing in it yet.

    Deliberately the same shape as a full one, so the screen does not
    need a second code path and cannot drift from it. The difference is
    in `source` and in the one decision, which is the only honest thing
    to put in front of someone whose business is not in here yet: what
    to do first.
    """
    return {
        "asof": as_of.isoformat(),
        "currency": currency,
        "source": "No data yet",
        "branches": ["ALL"],
        "line": {k: 0 for k in
                 ("on_order", "on_water", "at_port", "on_shelf", "idle",
                  "running_out")},
        "stock": [],
        "shipments": [],
        "decisions": [{
            "tone": "water",
            "segment": "on_shelf",
            "title": "Nothing here yet",
            "value": 0,
            "body": "Add your branches and the lines you carry, then a "
                    "receipt for what is on the shelf today. From there "
                    "every figure on this screen is yours.",
            "action": "Set up the first branch",
            "goto": "stock",
        }],
    }
