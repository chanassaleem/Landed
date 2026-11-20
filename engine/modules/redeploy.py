"""Module 4: redeployment between locations.

The highest-value output in the engine, and the one no single-entity ERP can
produce. One branch sits on seven months of cover while another is critical on
the same item. The answer is not a purchase order, it is a transfer: cash
released this week instead of cash committed at a 60-90 day lead time behind a
letter of credit.

Two guards, both deliberate:

  - A sending location is never stripped below its own cover floor to rescue
    another. Solving a stockout by creating one is not an improvement.
  - Trivial moves are not suggested. A transfer has handling cost, paperwork
    and breakage risk, so it has to clear a value floor to be worth a van.

Expiry-risk stock is treated as a preferred donor rather than an excluded one.
Stock that will expire unused at one location may be consumed comfortably at
another, which converts a certain write-off into a sale.

Demand, and therefore policy, is per location. A block carries its own
classification, so the same item can be smooth in Karachi and intermittent in
Lahore and is treated correctly in each.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from engine.modules.classify import key_for
from engine.schema import (
    ClassificationResult,
    InventoryResult,
    ItemSnapshot,
    StockStatus,
    Thresholds,
    TransferSuggestion,
    TransferUrgency,
)

DAYS_PER_MONTH = 30.0

# Receiving statuses that justify pulling stock from elsewhere, worst first.
_NEED = (StockStatus.CRITICAL, StockStatus.REORDER)

_URGENCY = {
    StockStatus.CRITICAL: TransferUrgency.URGENT,
    StockStatus.REORDER: TransferUrgency.USEFUL,
}


@dataclass(frozen=True)
class LocationBlock:
    """One location's own inventory position and its own demand classification.

    This is a computed intermediate rather than a canonical input record, so it
    lives with the module that consumes it rather than in schema.py. The caller
    builds one block per (entity, location) by running modules 1 and 3 over that
    location's rows only.
    """

    entity_id: str
    location_id: str
    items: Tuple[ItemSnapshot, ...]
    classes: Dict[tuple, ClassificationResult]
    results: Tuple[InventoryResult, ...]


def _index(block: LocationBlock):
    items = {i.item_key: i for i in block.items}
    results = {r.item_key: r for r in block.results}
    return items, results


def _donatable(
    item: ItemSnapshot,
    result: InventoryResult,
    cls: ClassificationResult,
    thresholds: Thresholds,
) -> float:
    """Units this location can give up without breaching its own cover floor."""
    monthly = cls.mean_monthly_demand

    if result.status == StockStatus.EXPIRY_RISK:
        # Everything that will not be consumed before expiry is free to move.
        days = result.days_to_expiry or 0
        consumable = (monthly / DAYS_PER_MONTH) * days
        return max(0.0, item.on_hand - consumable)

    if result.status != StockStatus.EXCESS:
        return 0.0

    if monthly <= 0:
        # No demand here at all, so nothing is needed locally. Keep none.
        return max(0.0, item.on_hand)

    # Floor at this location's own order-up-to level, not at a flat number of
    # months of cover. Order-up-to already carries safety stock sized to the
    # item's service target, which matters most for intermittent spares: their
    # mean monthly demand is near zero, so a cover-months floor would happily
    # ship out the last pump that nobody can predict needing.
    keep = max(result.order_up_to, thresholds.sender_min_cover_months * monthly)
    return max(0.0, item.on_hand - keep)


def suggest(
    blocks: Iterable[LocationBlock],
    thresholds: Thresholds | None = None,
) -> Tuple[TransferSuggestion, ...]:
    """Match surplus at one location to need at another, item by item."""
    thresholds = thresholds or Thresholds()
    blocks = list(blocks)

    # item_key -> list of (block, item, result, class)
    by_item: Dict[str, List[tuple]] = defaultdict(list)
    for b in blocks:
        items, results = _index(b)
        for key, item in items.items():
            res = results.get(key)
            # classes is keyed per (item, branch), so the item's own branch
            # has to go into the lookup. Keyed by item alone this silently
            # returned None and the item was skipped from redeployment.
            cls = b.classes.get(
                key_for(key, getattr(item, "location_id", ""))
            )
            if res is None or cls is None:
                continue
            by_item[key].append((b, item, res, cls))

    out: List[TransferSuggestion] = []

    for key, entries in by_item.items():
        if len(entries) < 2:
            continue  # one location only; nothing to redeploy

        donors = []
        for b, item, res, cls in entries:
            available = _donatable(item, res, cls, thresholds)
            if available > 0:
                donors.append([b, item, res, cls, available])

        receivers = [
            (b, item, res, cls)
            for b, item, res, cls in entries
            if res.status in _NEED and res.suggested_order_qty > 0
        ]
        if not donors or not receivers:
            continue

        # Worst need first, and within that the larger import avoided.
        receivers.sort(
            key=lambda e: (
                _NEED.index(e[2].status),
                -e[2].suggested_order_qty * e[1].unit_cost,
            )
        )
        # Give from the most over-stocked location first.
        donors.sort(key=lambda d: -d[4])

        for rb, ritem, rres, rcls in receivers:
            need = rres.suggested_order_qty
            for donor in donors:
                if need <= 0:
                    break
                db, ditem, dres, dcls, available = donor
                if available <= 0:
                    continue
                if db.entity_id == rb.entity_id and db.location_id == rb.location_id:
                    continue

                qty = float(min(available, need))
                unit_cost = ditem.unit_cost
                value = qty * unit_cost

                if value < thresholds.min_transfer_value:
                    continue

                donor[4] = available - qty
                need -= qty

                monthly = dcls.mean_monthly_demand
                remaining = ditem.on_hand - qty
                cover_after = (remaining / monthly) if monthly > 0 else None

                intercompany = db.entity_id != rb.entity_id
                urgency = _URGENCY.get(rres.status, TransferUrgency.OPTIONAL)

                if dres.status == StockStatus.EXPIRY_RISK:
                    why = (
                        f"{qty:,.0f} units at {db.location_id} expire before they "
                        f"can be used there, and {rb.location_id} needs them now. "
                        f"Moving them avoids a write-off and a purchase."
                    )
                elif urgency is TransferUrgency.URGENT:
                    why = (
                        f"{rb.location_id} runs out while {db.location_id} holds "
                        f"{dres.months_cover:.1f} months of cover. "
                        f"A transfer avoids PKR {value/1e6:.2f}M of imports."
                    )
                else:
                    why = (
                        f"{db.location_id} is above its cover limit and "
                        f"{rb.location_id} is at its reorder point. "
                        f"Transfer instead of ordering."
                    )

                if intercompany:
                    why += " Inter-company, so it needs a transfer invoice."

                out.append(
                    TransferSuggestion(
                        item_key=key,
                        description=ditem.description,
                        from_entity=db.entity_id,
                        from_location=db.location_id,
                        to_entity=rb.entity_id,
                        to_location=rb.location_id,
                        quantity=round(qty, 2),
                        unit_cost=unit_cost,
                        value_moved=round(value, 2),
                        import_avoided=round(qty * ritem.unit_cost, 2),
                        receiving_status=rres.status.value,
                        sending_cover_after=round(cover_after, 2)
                        if cover_after is not None
                        else None,
                        is_intercompany=intercompany,
                        urgency=urgency,
                        reason=why,
                    )
                )

    order = {
        TransferUrgency.URGENT: 0,
        TransferUrgency.USEFUL: 1,
        TransferUrgency.OPTIONAL: 2,
    }
    out.sort(key=lambda t: (order[t.urgency], -t.import_avoided))
    return tuple(out)
