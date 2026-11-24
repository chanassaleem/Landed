"""The four things that happen to stock in a day.

Receiving, selling, counting and moving between branches. Every one of
them is a row in the ledger and nothing else: there is no balance to
update, so there is no second number that can disagree with the first.

Each has a sign that is not negotiable. A receipt adds, a sale takes
away, a transfer does both at once. Letting the caller choose the sign
would mean a typo in a form could turn a sale into a delivery, and the
stock figure would be wrong by twice the quantity with nothing out of
place to notice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date


class NotAllowed(Exception):
    """The movement is refused, with a reason a person can act on."""


# reason -> (sign, needs a cost, what it is called on screen)
KINDS = {
    "grn":        (+1, True,  "Received"),
    "sale":       (-1, False, "Sold"),
    "issue":      (-1, False, "Issued"),
    "return_in":  (+1, False, "Returned by a customer"),
    "return_out": (-1, False, "Returned to the supplier"),
    "write_off":  (-1, False, "Written off"),
}

# An adjustment and a transfer are not in KINDS because neither is a
# quantity you add: an adjustment is a count you reconcile to, and a
# transfer is two movements that must both happen or neither.


@dataclass(frozen=True)
class Movement:
    item_id: str
    location_id: str
    qty: float
    reason: str
    unit_cost: float | None = None
    occurred_at: date | None = None
    note: str = ""


def _org(run) -> str:
    row = run.execute(
        "select id from public.organisations order by created_at limit 1"
    ).fetchone()
    if not row:
        raise NotAllowed("This account has no organisation yet.")
    return row[0]


def _resolve(run, org, sku: str, branch: str) -> tuple[str, str]:
    row = run.execute(
        "select i.id, l.id from public.items i, public.locations l "
        "where i.org_id = %s and l.org_id = %s and i.sku = %s and l.code = %s",
        (org, org, sku, branch),
    ).fetchone()
    if not row:
        raise NotAllowed(
            f"{sku} at {branch} is not set up. Check the SKU and the branch "
            f"code.")
    return row[0], row[1]


def record(run, sku: str, branch: str, kind: str, qty: float,
           unit_cost: float | None = None, when: date | None = None,
           note: str = "") -> dict:
    """One movement, one row.

    `qty` is always given as a positive number and the sign comes from
    the kind. A form that could send a negative sale would make the
    ledger wrong by twice the quantity, with nothing out of place for
    anyone to notice.
    """
    if kind not in KINDS:
        raise NotAllowed(f"{kind!r} is not something that happens to stock.")
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        raise NotAllowed("The quantity is not a number.") from None
    if qty <= 0:
        raise NotAllowed("The quantity has to be more than zero.")

    sign, wants_cost, _label = KINDS[kind]

    # A receipt with no cost is not a receipt anybody can value.
    #
    # This was a preference, not a rule: the kind knew it wanted a
    # cost and nothing checked. The movement went in with a null
    # unit_cost, public.item_cost skips null-cost movements when it
    # averages, and the on-hand figure does not -- so a hundred units
    # received without a price were valued at the average of the
    # hundred that had one. Received 100 at 1,000 and 100 more with
    # the field left blank, and the stock read 200,000 against
    # 100,000 actually spent.
    #
    # Zero is a cost: a free sample and a warranty replacement are
    # both real, and both are stated. What is refused is silence.
    if wants_cost and unit_cost is None:
        raise NotAllowed(
            "A receipt needs a cost each. It is what the line is "
            "valued at, and a receipt without one quietly takes the "
            "price of whatever else is on the shelf. Enter 0 if it "
            "genuinely cost nothing.")

    org = _org(run)
    item_id, location_id = _resolve(run, org, sku, branch)

    if sign < 0:
        on_hand = run.execute(
            "select coalesce(sum(qty), 0) from public.stock_movement "
            "where item_id = %s and location_id = %s",
            (item_id, location_id)).fetchone()[0]
        if float(on_hand) < qty:
            # Refused rather than allowed to go negative. A negative
            # shelf is always a mistake somewhere -- an unrecorded
            # receipt, usually -- and letting it through hides which.
            raise NotAllowed(
                f"There are only {float(on_hand):,.0f} on the shelf at "
                f"{branch}. Record the receipt that is missing, or use "
                f"Adjust count if the shelf disagrees with the ledger.")

    run.execute(
        "insert into public.stock_movement (org_id, item_id, location_id, "
        "qty, unit_cost, reason, occurred_at, note, client_ref) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (org, item_id, location_id, sign * qty,
         # `and unit_cost` would have written a stated zero as null,
         # which is the same silence this refuses above.
         unit_cost if wants_cost else None,
         kind, when or date.today(), note or None, str(uuid.uuid4())))
    return {"sku": sku, "branch": branch, "reason": kind, "qty": sign * qty}


def adjust(run, sku: str, branch: str, counted: float,
           when: date | None = None, note: str = "") -> dict:
    """Reconcile the ledger to a physical count.

    The person types what is on the shelf, not the difference. Asking
    for a difference asks them to do arithmetic against a number they
    cannot see, and get the sign right as well -- which is how a count
    of 48 becomes a correction of -48.
    """
    try:
        counted = float(counted)
    except (TypeError, ValueError):
        raise NotAllowed("The count is not a number.") from None
    if counted < 0:
        raise NotAllowed("A count cannot be negative.")

    org = _org(run)
    item_id, location_id = _resolve(run, org, sku, branch)
    on_hand = float(run.execute(
        "select coalesce(sum(qty), 0) from public.stock_movement "
        "where item_id = %s and location_id = %s",
        (item_id, location_id)).fetchone()[0])

    delta = counted - on_hand
    if delta == 0:
        return {"sku": sku, "branch": branch, "reason": "adjustment",
                "qty": 0, "note": "the ledger already agreed"}

    run.execute(
        "insert into public.stock_movement (org_id, item_id, location_id, "
        "qty, reason, occurred_at, note, client_ref) "
        "values (%s, %s, %s, %s, 'adjustment', %s, %s, %s)",
        (org, item_id, location_id, delta, when or date.today(),
         note or f"counted {counted:,.0f}, ledger said {on_hand:,.0f}",
         str(uuid.uuid4())))
    return {"sku": sku, "branch": branch, "reason": "adjustment",
            "qty": delta, "was": on_hand, "now": counted}


def transfer(run, sku: str, from_branch: str, to_branch: str, qty: float,
             when: date | None = None, note: str = "") -> dict:
    """Move stock between branches: two rows, both or neither.

    Written as a pair in one statement so there is no moment where the
    stock has left one branch and not arrived at the other. On a ledger
    that moment is not a race condition, it is a wrong number that
    somebody reads.
    """
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        raise NotAllowed("The quantity is not a number.") from None
    if qty <= 0:
        raise NotAllowed("The quantity has to be more than zero.")
    if from_branch == to_branch:
        raise NotAllowed("Those are the same branch.")

    org = _org(run)
    item_id, out_id = _resolve(run, org, sku, from_branch)
    _item, in_id = _resolve(run, org, sku, to_branch)

    on_hand = float(run.execute(
        "select coalesce(sum(qty), 0) from public.stock_movement "
        "where item_id = %s and location_id = %s",
        (item_id, out_id)).fetchone()[0])
    if on_hand < qty:
        raise NotAllowed(
            f"There are only {on_hand:,.0f} at {from_branch} to send.")

    pair = str(uuid.uuid4())
    run.executemany(
        "insert into public.stock_movement (org_id, item_id, location_id, "
        "qty, reason, occurred_at, note, client_ref) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s)",
        [
            (org, item_id, out_id, -qty, "transfer_out", when or date.today(),
             note or f"to {to_branch}", f"{pair}:out"),
            (org, item_id, in_id, qty, "transfer_in", when or date.today(),
             note or f"from {from_branch}", f"{pair}:in"),
        ])
    return {"sku": sku, "from": from_branch, "to": to_branch, "qty": qty}


def history(run, sku: str, branch: str, limit: int = 25) -> list[dict]:
    """What has happened to this line, most recent first."""
    org = _org(run)
    item_id, location_id = _resolve(run, org, sku, branch)
    rows = run.execute(
        "select occurred_at, qty, reason, unit_cost, note "
        "from public.stock_movement "
        "where item_id = %s and location_id = %s "
        "order by occurred_at desc, created_at desc limit %s",
        (item_id, location_id, limit)).fetchall()
    return [
        {"on": str(r[0]), "qty": float(r[1]), "reason": r[2],
         "unit_cost": float(r[3]) if r[3] is not None else None,
         "note": r[4] or ""}
        for r in rows
    ]
