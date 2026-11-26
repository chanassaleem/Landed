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


def _hold(run, org, item_id, location_id) -> None:
    """Hold this line at this branch for the rest of the transaction.

    Two sales at once both read the same on-hand figure, both find it
    sufficient and both insert. Proven on a clean database: 100 units
    on the shelf, two sales of 80 started together, both committed,
    and the ledger finished at -60 -- which the check above exists to
    make impossible and which nothing errors on, because each
    transaction was correct when it looked.

    An advisory lock rather than SELECT ... FOR UPDATE because there
    is no row to lock: the on-hand figure is a sum of movements, and
    item_locations may have no row for a line that has only ever
    arrived. The lock is on the thing being protected -- this item at
    this branch -- so two branches, or two items, never wait for each
    other. It is transaction-scoped, so it is released by the commit
    or the rollback and cannot be leaked by a failure.
    """
    run.execute(
        "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"{org}:{item_id}:{location_id}",))


def record(run, sku: str, branch: str, kind: str, qty: float,
           unit_cost: float | None = None, when: date | None = None,
           note: str = "", client_ref: str | None = None) -> dict:
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

    # Receiving is a document, not a bare row, so it goes the long way
    # round even when the caller gave nothing but a quantity and a
    # cost. Writing the movement here instead left the stock on the
    # shelf and the order still reading its full quantity outstanding,
    # because outstanding is the ordered quantity less what the
    # receipt lines account for -- and there were none. Every caller
    # that already received this way gets a traceable receipt without
    # changing how it calls.
    if kind == "grn":
        return receive(run, sku, branch, qty, unit_cost, when=when,
                       note=note, client_ref=client_ref)

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

    # Before the check, not after it: a lock taken afterwards protects
    # nothing, because the figure it was checking is already stale.
    if sign < 0:
        _hold(run, org, item_id, location_id)

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

    # A retry must not post twice.
    #
    # The ledger has had a unique index on (org_id, client_ref) since
    # it was written, and this generated a fresh uuid every call -- so
    # the guarantee was there and nothing used it. A browser that
    # retries a request whose reply was lost, or somebody pressing a
    # button twice on a slow connection, posted the movement again.
    #
    # Where the caller supplies a key the insert is idempotent: the
    # second attempt conflicts, writes nothing, and reports what the
    # first one did rather than an error about a duplicate.
    ref = client_ref or str(uuid.uuid4())
    done = run.execute(
        "insert into public.stock_movement (org_id, item_id, location_id, "
        "qty, unit_cost, reason, occurred_at, note, client_ref) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        # The predicate is repeated because the index is partial;
        # without it Postgres cannot match the conflict target.
        "on conflict (org_id, client_ref) where client_ref is not null "
        "do nothing "
        "returning id",
        (org, item_id, location_id, sign * qty,
         # `and unit_cost` would have written a stated zero as null,
         # which is the same silence this refuses above.
         unit_cost if wants_cost else None,
         kind, when or date.today(), note or None, ref)).fetchone()
    return {"sku": sku, "branch": branch, "reason": kind, "qty": sign * qty,
            "recorded": done is not None}


def receive(run, sku: str, branch: str, qty: float,
            unit_cost: float | None = None, *,
            po: str = "", shipment: str = "", batch: str = "",
            expiry: date | None = None, when: date | None = None,
            weight: float | None = None, volume: float | None = None,
            currency: str = "", fx: float | None = None,
            grn: str = "", note: str = "",
            client_ref: str | None = None) -> dict:
    """Goods arriving, as a document rather than a bare ledger row.

    Receiving through `record` wrote a movement and nothing else. The
    stock appeared on the shelf, and three things that should have
    followed from it did not. Proven on a clean database: order 300,
    receive 100 by hand.

      movement: reason grn, qty 100, source_type null, source_id null
      goods_receipts: 0 rows
      grn_lines: 0 rows
      the order still reads 300 outstanding

    The third is the expensive one. Outstanding quantity is the ordered
    quantity less what the receipts account for, so a receipt that
    files no line never reduces it -- and the Ordering screen goes on
    recommending stock that is already in the warehouse. In a trade
    where one order is months of cash, that is the mistake that costs
    the most.

    The first two are what make the rest of the workflow possible.
    Freight is allocated across the lines of a shipment, and a
    shipment's lines are joined to receipt lines: a receipt with no
    line can never carry freight, so its landed cost can only ever be
    the supplier's price. And a movement with no source cannot be
    traced back to what caused it, which is the one question an
    auditor asks.

    So this writes the document and lets the trigger post the
    movement, which is where that rule already lives -- an import, a
    phone and a script cannot each forget it in their own way.

    Manual and shipment-linked receiving stay distinguishable without
    a flag to keep in step: a receipt is shipment-linked exactly when a
    shipment line joins to it. A stored flag could disagree with the
    join; a join cannot disagree with itself.
    """
    org = _org(run)
    item_id, location_id = _resolve(run, org, sku, branch)
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        raise NotAllowed("The quantity is not a number.") from None
    if qty <= 0:
        raise NotAllowed(
            f"{sku}: a receipt of {qty:g} is not a receipt. To take stock "
            f"off the shelf, record a return to the supplier or a write-off.")

    # The order this is against, and the line on it for this item.
    po_id = po_line_id = None
    order_price = order_ccy = order_fx = None
    if po:
        row = run.execute(
            "select id, currency, fx_rate from public.purchase_orders"
            " where org_id = %s and po_number = %s", (org, po)).fetchone()
        if not row:
            raise NotAllowed(f"There is no order numbered {po}.")
        po_id, order_ccy, order_fx = row
        line = run.execute(
            "select id, unit_price from public.po_lines"
            " where org_id = %s and po_id = %s and item_id = %s"
            " order by created_at limit 1",
            (org, po_id, item_id)).fetchone()
        if not line:
            raise NotAllowed(
                f"{po} has no line for {sku}. Receiving an item nobody "
                f"ordered is a receipt without an order, not a receipt "
                f"against this one.")
        po_line_id, order_price = line

    # A receipt without a cost puts stock on the shelf that the
    # valuation counts at nothing. Where there is an order to take the
    # price from, take it; otherwise say so rather than guessing.
    if unit_cost is None and order_price is not None:
        unit_cost = float(order_price)
    if unit_cost is None:
        raise NotAllowed(
            f"{sku}: a receipt needs a cost each, and there is no order "
            f"here to take one from. Enter 0 if it genuinely cost nothing.")
    unit_cost = float(unit_cost)
    if unit_cost < 0:
        raise NotAllowed("A cost cannot be negative.")

    # What currency that cost is in.
    #
    # Against an order, the order says, and the database inherits both
    # the currency and the rate. Without one, a bare number was taken
    # as rupees in silence -- so a supplier's euro price typed into a
    # manual receipt valued the stock at a three-hundredth of what it
    # cost. This project has already paid for that once: the sample
    # generator priced euro orders at rupee figures, and nothing
    # noticed until a later change made the ledger multiply.
    #
    # So a foreign currency has to arrive with the rate it was bought
    # at. Not looked up -- a rate looked up later is not the rate the
    # money changed hands at, and the difference is the importer's
    # margin.
    base = run.execute(
        "select currency from public.organisations where id = %s",
        (org,)).fetchone()[0]
    currency = (currency or "").strip().upper()
    if currency and currency != base and fx is None and po_line_id is None:
        raise NotAllowed(
            f"{sku}: a cost in {currency} needs the rate it was bought "
            f"at, or an order to take the rate from. Without one there "
            f"is no way to tell {unit_cost:g} {currency} from "
            f"{unit_cost:g} {base}.")
    if fx is not None:
        try:
            fx = float(fx)
        except (TypeError, ValueError):
            raise NotAllowed("The exchange rate is not a number.") from None
        if fx <= 0:
            raise NotAllowed("An exchange rate has to be above zero.")
    # Stated beats inherited: somebody typing the rate on the receipt
    # is recording what this consignment actually cost, which is the
    # whole point of holding a rate per receipt rather than per order.
    line_ccy = currency or order_ccy
    line_fx = fx if fx is not None else order_fx

    # The receipt's own number, which is what says two uploads or two
    # button presses are the same arrival. Derived from the caller's
    # idempotency key when they did not name one, so that a retry
    # lands on the same number and the identity below recognises it.
    if not grn:
        stem = (client_ref or str(uuid.uuid4())).replace("-", "")[:8].upper()
        grn = f"GRN-{stem}"

    run.execute(
        "insert into public.goods_receipts (org_id, location_id, po_id,"
        " grn_number, received_at, note) values (%s,%s,%s,%s,%s,%s)"
        " on conflict (org_id, grn_number) do nothing",
        (org, location_id, po_id, grn, when or date.today(), note or None))
    grn_id = run.execute(
        "select id from public.goods_receipts where org_id = %s"
        " and grn_number = %s", (org, grn)).fetchone()[0]

    # Identity is the receipt, the item and the batch -- the same three
    # the importer uses. Two lots of one item on one arrival are two
    # lines; the same lot twice is one.
    already = run.execute(
        "select id from public.grn_lines where org_id = %s and grn_id = %s"
        " and item_id = %s and coalesce(batch,'') = coalesce(%s,'')",
        (org, grn_id, item_id, batch or None)).fetchone()
    if already:
        return {"sku": sku, "branch": branch, "qty": qty, "grn": grn,
                "po": po or None, "shipment": shipment or None,
                "recorded": False, "linked": bool(shipment)}

    line_id = run.execute(
        "insert into public.grn_lines (org_id, grn_id, po_line_id, item_id,"
        " qty, unit_price, expiry_date, batch, currency, fx_rate)"
        " values (%s,%s,%s,%s,%s,%s,%s,%s,%s, coalesce(%s, 1))"
        " returning id",
        (org, grn_id, po_line_id, item_id, qty, unit_cost, expiry,
         batch or None, line_ccy, line_fx)).fetchone()[0]

    if shipment:
        ship = run.execute(
            "select id from public.shipments where org_id = %s"
            " and reference = %s", (org, shipment)).fetchone()
        if not ship:
            raise NotAllowed(f"There is no shipment referenced {shipment}.")
        # Weight and volume live on the shipment line because that is
        # what freight is divided by. Left out here they are null, and
        # the allocation says so rather than dividing by nothing.
        run.execute(
            "insert into public.shipment_lines (shipment_id, grn_line_id,"
            " org_id, weight_kg, volume_cbm) values (%s,%s,%s,%s,%s)"
            " on conflict (shipment_id, grn_line_id) do nothing",
            (ship[0], line_id, org, weight, volume))

    return {"sku": sku, "branch": branch, "qty": qty, "grn": grn,
            "po": po or None, "shipment": shipment or None,
            "unit_cost": unit_cost, "recorded": True,
            "linked": bool(shipment)}


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
    # A count reconciles to the figure it read. Without the lock, two
    # counts taken together each write a delta against the same
    # starting figure and the shelf ends up at neither number.
    _hold(run, org, item_id, location_id)
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
    # Both ends, and always in the same order. Two transfers moving
    # the same line in opposite directions would otherwise each hold
    # what the other is waiting for, and neither would ever finish.
    for lid in sorted((str(out_id), str(in_id))):
        _hold(run, org, item_id, lid)

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
