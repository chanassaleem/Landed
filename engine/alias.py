"""Applying alias mappings before anything is computed.

Detection without resolution is half a feature. engine/sanity.py can tell you
that PIMS and PIMS-2 look like one hospital; this is what happens once someone
says yes. The mapping is applied to the records on the way in, so every module
downstream sees one customer, and group exposure, the ageing and the cash
timing are all computed on the merged figure.

Applied on load rather than stored into the files, because the files are
immutable snapshots of what the customer's system actually exported. The
mapping is a decision about how to read them, and decisions belong beside the
data rather than inside it. Change the mapping and re-run and every past
figure moves with it, which is the behaviour you want: the alias was always
true, you just had not recorded it yet.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Sequence, Tuple


def _remap(records: Sequence, field: str, mapping: Dict[str, str]) -> Tuple[list, int]:
    if not mapping or not records:
        return list(records), 0
    out, changed = [], 0
    for r in records:
        old = getattr(r, field, None)
        new = mapping.get(old)
        if new and new != old:
            out.append(replace(r, **{field: new}))
            changed += 1
        else:
            out.append(r)
    return out, changed


def apply(
    items: Sequence = (),
    demand: Sequence = (),
    invoices: Sequence = (),
    jobs: Sequence = (),
    placements: Sequence = (),
    item_map: Dict[str, str] | None = None,
    customer_map: Dict[str, str] | None = None,
    machine_map: Dict[str, str] | None = None,
) -> dict:
    """Rewrite keys through the mappings. Returns the records and a count of
    what moved, so the UI can say what the mapping did rather than leaving it
    invisible."""
    item_map = item_map or {}
    customer_map = customer_map or {}
    machine_map = machine_map or {}

    items, a = _remap(items, "item_key", item_map)
    demand, b = _remap(demand, "item_key", item_map)
    invoices, c = _remap(invoices, "customer_key", customer_map)
    placements, d = _remap(placements, "customer_key", customer_map)
    placements, e = _remap(placements, "machine_key", machine_map)
    jobs, f = _remap(jobs, "machine_key", machine_map)

    return {
        "items": items,
        "demand": demand,
        "invoices": invoices,
        "jobs": jobs,
        "placements": placements,
        "applied": {
            "item_rows": a + b,
            "customer_rows": c + d,
            "machine_rows": e + f,
            "total": a + b + c + d + e + f,
        },
    }
