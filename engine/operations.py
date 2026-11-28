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
from collections.abc import Mapping, Sequence
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
    if currency and currency != base and fx is None:
        # An order supplies a rate only for its own currency.
        #
        # This asked whether there was an order, not whether the order
        # was in the same money. An order in euro at 305, a
        # consignment on it invoiced in dollars, no rate stated, and
        # the line took 305 -- so 60 USD was valued at 18,300 rather
        # than about 16,800. Nine per cent on that pair; a yen price
        # at a euro rate is wrong by a factor of three hundred.
        if order_ccy is None or order_ccy == currency:
            pass
        else:
            raise NotAllowed(
                f"{sku}: this receipt is in {currency} and {po} is "
                f"priced in {order_ccy}. The order's rate of "
                f"{float(order_fx):g} was quoted for {order_ccy}, so it "
                f"cannot value {currency}. Enter the rate this "
                f"consignment was bought at.")
    if currency and currency != base and fx is None and order_ccy is None:
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
    #
    # The rate is inherited only for the currency it was quoted for.
    # Taking the order's currency and the order's rate together is
    # right; taking its rate for a different currency is the bug
    # above. The database refuses that too, since the importer writes
    # these lines itself.
    line_ccy = currency or order_ccy
    if fx is not None:
        line_fx = fx
    elif line_ccy is not None and line_ccy == order_ccy:
        line_fx = order_fx
    else:
        line_fx = None

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
            "select s.id, s.supplier_id, sup.name"
            "  from public.shipments s"
            "  left join public.suppliers sup on sup.id = s.supplier_id"
            " where s.org_id = %s and s.reference = %s",
            (org, shipment)).fetchone()
        if not ship:
            raise NotAllowed(f"There is no shipment referenced {shipment}.")

        # The order and the shipment have to have something to do with
        # each other.
        #
        # Nothing checked this. PO-1001 from Supplier A, received onto
        # SHP-01 which is Supplier B's, was accepted without
        # complaint, and Supplier B's freight was then allocated to
        # Supplier A's inventory -- both suppliers' costs wrong, in
        # opposite directions, feeding the unit cost the price list is
        # built on.
        #
        # Only a contradiction is refused. A shipment with no supplier
        # recorded is a forwarder's consolidation, which is the honest
        # way to hold a container of two suppliers' goods, and it
        # stays open.
        if po_id is not None and ship[1] is not None:
            po_supplier = run.execute(
                "select supplier_id, (select name from public.suppliers"
                "  where id = purchase_orders.supplier_id)"
                "  from public.purchase_orders where id = %s",
                (po_id,)).fetchone()
            carrying = run.execute(
                "select 1 from public.shipment_orders"
                " where shipment_id = %s and po_id = %s",
                (ship[0], po_id)).fetchone()
            if (not carrying and po_supplier[0] is not None
                    and po_supplier[0] != ship[1]):
                raise NotAllowed(
                    f"{shipment} is {ship[2]}'s shipment and {po} is "
                    f"{po_supplier[1]}'s order. Receiving one "
                    f"supplier's goods onto another's shipment puts "
                    f"their freight on the wrong inventory. If this "
                    f"container really is carrying both, clear the "
                    f"supplier on the shipment.")

        # Recorded, not demanded: the link is written the first time
        # goods arrive against the pair, so nobody has to declare a
        # manifest before they can book in a delivery.
        if po_id is not None:
            run.execute(
                "insert into public.shipment_orders (org_id, shipment_id,"
                " po_id) values (%s,%s,%s)"
                " on conflict (shipment_id, po_id) do nothing",
                (org, ship[0], po_id))
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


def correct_receipt(run, grn: str, sku: str, *, batch: str = "",
                    qty: float | None = None,
                    unit_cost: float | None = None,
                    expiry: date | None = None,
                    fx: float | None = None,
                    why: str = "") -> dict:
    """Put right a receipt that was wrong, without rewriting it.

    A receipt is an event. The stock went on the shelf, the valuation
    took the cost, and a month of figures was built on both. When the
    warehouse says the pallet held ninety rather than a hundred, the
    answer is not to edit the hundred away: somebody reconciled
    against that hundred, and a figure that changes under them with
    nothing to show for it is worse than a figure that was wrong.

    So three rows. The original line stays as it was filed. A
    correction line carries the right figures and points at the line
    it replaces. The ledger gets a reversal of the original movement
    beside the corrected one, so it still nets to what is on the
    shelf and still only ever has rows added to it.

    The superseded line stops counting everywhere at once -- the
    outstanding quantity on the order, the basis freight is divided
    by, the valuation -- because a correction that reaches one of
    those and not the others is worse than no correction at all. That
    is what grn_lines_live is for, and all three readers use it.
    """
    org = _org(run)
    why = (why or "").strip()
    if not why:
        raise NotAllowed(
            "A correction needs a reason. Without one it is an "
            "unexplained change to a figure somebody has already "
            "reconciled against.")

    line = run.execute(
        "select gl.id, gl.grn_id, gl.po_line_id, gl.item_id, gl.qty,"
        "       gl.unit_price, gl.expiry_date, gl.batch, gl.currency,"
        "       gl.fx_rate"
        "  from public.grn_lines gl"
        "  join public.goods_receipts g on g.id = gl.grn_id"
        "  join public.items i on i.id = gl.item_id"
        " where gl.org_id = %s and g.grn_number = %s and i.sku = %s"
        "   and coalesce(gl.batch,'') = coalesce(%s,'')",
        (org, grn, sku, batch or None)).fetchone()
    if not line:
        where = f" batch {batch}" if batch else ""
        raise NotAllowed(
            f"{grn} has no line for {sku}{where}. Check the receipt "
            f"number and the batch: a correction has to name the line "
            f"it is replacing.")
    (line_id, grn_id, po_line_id, item_id, was_qty, was_price,
     was_expiry, line_batch, line_ccy, line_fx) = line

    done = run.execute(
        "select id from public.grn_lines where supersedes = %s",
        (line_id,)).fetchone()
    if done:
        raise NotAllowed(
            f"That line on {grn} has already been corrected. Correct "
            f"the correction instead, so the chain stays readable.")

    new_qty = float(was_qty) if qty is None else float(qty)
    new_price = float(was_price) if unit_cost is None else float(unit_cost)
    new_fx = float(line_fx) if fx is None else float(fx)
    new_expiry = was_expiry if expiry is None else expiry
    if new_qty <= 0:
        raise NotAllowed(
            f"A corrected receipt of {new_qty:g} is not a receipt. If "
            f"nothing arrived, reverse it as a return to the supplier "
            f"or a write-off, which says what became of the goods.")
    if new_price < 0:
        raise NotAllowed("A cost cannot be negative.")
    if new_fx <= 0:
        raise NotAllowed("An exchange rate has to be above zero.")

    if (new_qty == float(was_qty) and new_price == float(was_price)
            and new_fx == float(line_fx) and new_expiry == was_expiry):
        raise NotAllowed(
            "That correction changes nothing. Give the figure that is "
            "wrong and what it should be.")

    location_id = run.execute(
        "select location_id from public.goods_receipts where id = %s",
        (grn_id,)).fetchone()[0]
    _hold(run, org, item_id, location_id)

    # Withdraw the original movement. A reversal, not a deletion: the
    # ledger is append-only, and the row that was wrong is part of why
    # the figures said what they said.
    original = run.execute(
        "select id, unit_cost from public.stock_movement"
        " where org_id = %s and source_type = 'grn_line' and source_id = %s",
        (org, line_id)).fetchone()
    # The reversal carries the movement's own cost, not the landed cost
    # the original contributed to the valuation. That looks wrong and
    # is not, and the reason is worth writing down because the first
    # attempt here got it the other way round.
    #
    # item_cost prefers a line's landed cost over the movement's cost,
    # joining through the receipt line. Superseding the original takes
    # it out of grn_lines_live, which landed_cost is built over, so the
    # original movement stops joining to a landed cost in the same
    # breath -- it falls back to its own 17,080. Withdrawing 22,680
    # against a contribution that is now 17,080 moves the valuation by
    # the freight. Withdrawing 17,080 cancels exactly, and the
    # corrected line brings its own freight back through the join.
    #
    # Caught by correcting a price on a shipment-linked receipt and
    # asking what the stock was then worth.
    if original:
        run.execute(
            "insert into public.stock_movement (org_id, location_id,"
            " item_id, qty, unit_cost, reason, source_type, source_id,"
            " occurred_at, note) values (%s,%s,%s,%s,%s,'reversal',"
            " 'movement',%s, now(), %s)",
            (org, location_id, item_id, -float(was_qty), original[1],
             original[0],
             f"Corrects {grn}"
             f"{' batch ' + line_batch if line_batch else ''}: {why}"))

    # The corrected line, on the same receipt, pointing at what it
    # replaces. Its own insert posts the corrected movement, through
    # the same trigger as every other receipt.
    new_id = run.execute(
        "insert into public.grn_lines (org_id, grn_id, po_line_id, item_id,"
        " qty, unit_price, expiry_date, batch, currency, fx_rate,"
        " supersedes, note) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " returning id",
        (org, grn_id, po_line_id, item_id, new_qty, new_price, new_expiry,
         line_batch, line_ccy, new_fx, line_id, why)).fetchone()[0]

    # The correction takes the original's place on its shipment, or it
    # would carry no freight and the charges would stop reconciling.
    run.execute(
        "insert into public.shipment_lines (shipment_id, grn_line_id,"
        " org_id, weight_kg, volume_cbm)"
        " select shipment_id, %s, org_id, weight_kg, volume_cbm"
        "   from public.shipment_lines where grn_line_id = %s"
        " on conflict (shipment_id, grn_line_id) do nothing",
        (new_id, line_id))

    changed = {}
    if new_qty != float(was_qty):
        changed["qty"] = (float(was_qty), new_qty)
    if new_price != float(was_price):
        changed["unit_cost"] = (float(was_price), new_price)
    if new_fx != float(line_fx):
        changed["fx"] = (float(line_fx), new_fx)
    if new_expiry != was_expiry:
        changed["expiry"] = (str(was_expiry), str(new_expiry))

    return {"grn": grn, "sku": sku, "batch": line_batch or None,
            "corrected": line_id, "line": new_id,
            "changed": changed, "why": why}


def set_shipment_basis(run, shipment: str, sku: str, *, batch: str = "",
                       weight: float | None = None,
                       volume: float | None = None,
                       why: str = "") -> dict:
    """Enter or correct the weight and volume freight is divided by.

    These are the two figures a charge is apportioned on, so changing
    one moves money between items. Entering a missing one is what
    rescues a charge that reached nothing: a bill divided by volume
    across lines with no volume recorded divides by zero, allocates to
    nothing, and leaves the importer pricing stock short by the whole
    bill with nothing on screen to say so.

    The allocation is a view over the current figures, so the division
    that produced last month's landed cost is gone the moment a figure
    changes. That is why the change is written down: afterwards, the
    audit row is the only thing that can explain the old number.
    """
    org = _org(run)
    why = (why or "").strip()
    if weight is None and volume is None:
        raise NotAllowed("Give a weight, a volume, or both.")
    if not why:
        raise NotAllowed(
            "Say why. Weight and volume decide how freight is split "
            "between items, so a change here moves money with nothing "
            "to explain it afterwards.")

    row = run.execute(
        "select sl.shipment_id, sl.grn_line_id, sl.weight_kg, sl.volume_cbm"
        "  from public.shipment_lines sl"
        "  join public.shipments s on s.id = sl.shipment_id"
        "  join public.grn_lines_live gl on gl.id = sl.grn_line_id"
        "  join public.items i on i.id = gl.item_id"
        " where sl.org_id = %s and s.reference = %s and i.sku = %s"
        "   and coalesce(gl.batch,'') = coalesce(%s,'')",
        (org, shipment, sku, batch or None)).fetchone()
    if not row:
        where = f" batch {batch}" if batch else ""
        raise NotAllowed(
            f"{shipment} has no received line for {sku}{where}. Freight "
            f"is divided across the goods that arrived, so there has to "
            f"be a receipt against the shipment before there is "
            f"anything to weigh.")
    ship_id, line_id, was_weight, was_volume = row

    changed = {}
    for field, value, was in (("weight_kg", weight, was_weight),
                              ("volume_cbm", volume, was_volume)):
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise NotAllowed(f"The {field.split('_')[0]} is not a "
                             f"number.") from None
        # Zero is a figure and null is not. A line that genuinely
        # weighs nothing still has to be stated, because that is what
        # tells the allocation to give it no share -- rather than
        # leaving the whole shipment with no basis at all.
        if value < 0:
            raise NotAllowed("A weight or a volume cannot be negative.")
        if was is not None and float(was) == value:
            continue
        run.execute(
            f"update public.shipment_lines set {field} = %s"
            " where shipment_id = %s and grn_line_id = %s",
            (value, ship_id, line_id))
        run.execute(
            "insert into public.shipment_line_changes (org_id, shipment_id,"
            " grn_line_id, field, was, became, why)"
            " values (%s,%s,%s,%s,%s,%s,%s)",
            (org, ship_id, line_id, field,
             float(was) if was is not None else None, value, why))
        changed[field] = (float(was) if was is not None else None, value)

    if not changed:
        raise NotAllowed(
            "That changes nothing. The line already holds those figures.")
    return {"shipment": shipment, "sku": sku, "batch": batch or None,
            "changed": changed, "why": why}


def adjust(run, sku: str, branch: str, counted: float,
           when: date | None = None, note: str = "",
           client_ref: str | None = None) -> dict:
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
        # Same shape as every other return from here. It was missing
        # `recorded`, so a caller reading that field got a KeyError on
        # the one path where nothing was written -- which is exactly
        # the path a caller asks about.
        return {"sku": sku, "branch": branch, "reason": "adjustment",
                "qty": 0, "was": on_hand, "now": counted, "recorded": False,
                "note": "the ledger already agreed"}

    # A retry must not post a second correction.
    #
    # This generated a fresh uuid every call, like the receipt and the
    # sale used to. A count submitted twice -- a lost reply, a double
    # press -- wrote two deltas against the same starting figure, and
    # the shelf ended up further from the count than before anybody
    # counted it.
    ref = client_ref or str(uuid.uuid4())
    done = run.execute(
        "insert into public.stock_movement (org_id, item_id, location_id, "
        "qty, reason, occurred_at, note, client_ref) "
        "values (%s, %s, %s, %s, 'adjustment', %s, %s, %s) "
        "on conflict (org_id, client_ref) where client_ref is not null "
        "do nothing returning id",
        (org, item_id, location_id, delta, when or date.today(),
         note or f"counted {counted:,.0f}, ledger said {on_hand:,.0f}",
         ref)).fetchone()
    return {"sku": sku, "branch": branch, "reason": "adjustment",
            "qty": delta, "was": on_hand, "now": counted,
            "recorded": done is not None}


def transfer(run, sku: str, from_branch: str, to_branch: str, qty: float,
             when: date | None = None, note: str = "",
             client_ref: str | None = None) -> dict:
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

    # Both sides keyed off one value, so a retry is refused as a pair.
    #
    # The pair was a fresh uuid each call, so a transfer whose reply
    # was lost moved the stock twice when it was sent again. Keyed off
    # the caller's own value, the second attempt conflicts on both
    # rows and writes neither -- which is the same both-or-neither
    # rule the operation already holds for the first attempt.
    pair = client_ref or str(uuid.uuid4())
    # Two execute calls, not executemany.
    #
    # Everything else here takes `run` as either a connection or a
    # cursor -- the tests pass a cursor, the API passes a connection
    # -- and psycopg's Connection has execute but not executemany. So
    # this was the one operation that worked everywhere except through
    # the endpoint a person actually uses, failing with "'Connection'
    # object has no attribute 'executemany'". Found by walking the
    # whole workflow over HTTP rather than in Python.
    #
    # Both or neither still holds: they are two statements in one
    # transaction, and the caller's commit is what makes either real.
    wrote = []
    for location, signed, reason, towards in (
            (out_id, -qty, "transfer_out", f"to {to_branch}"),
            (in_id, qty, "transfer_in", f"from {from_branch}")):
        wrote.append(run.execute(
            "insert into public.stock_movement (org_id, item_id, "
            "location_id, qty, reason, occurred_at, note, client_ref) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s) "
            "on conflict (org_id, client_ref) where client_ref is not null "
            "do nothing returning id",
            (org, item_id, location, signed, reason, when or date.today(),
             note or towards,
             f"{pair}:{'out' if signed < 0 else 'in'}")).fetchone())
    # Both or neither, on a retry as on the first attempt. One side
    # conflicting and the other not would mean the two rows had
    # different keys, which cannot happen -- but if it ever did, half
    # a transfer is the one outcome this operation exists to prevent.
    if any(wrote) and not all(wrote):
        raise NotAllowed(
            "Only one side of that transfer could be recorded, so "
            "neither was kept. Try it again.")
    return {"sku": sku, "from": from_branch, "to": to_branch, "qty": qty,
            "recorded": all(wrote)}


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


def raise_order(run, po_number: str, *, branch: str, supplier: str = "",
                currency: str = "", fx: float | None = None,
                expected: str | None = None, ordered: str | None = None,
                note: str = "", lines: Sequence[Mapping] = ()) -> dict:
    """Place a purchase order, with its lines, in one transaction.

    Until this existed an order arrived one way: a row in the import
    workbook. That is fine for the first load of history and wrong
    for everything after it. A buyer who has just placed an order on
    the phone is not going to open Excel, add a row to "On order" and
    re-import the file -- so the order goes unrecorded, and the
    outstanding quantity that the whole Ordering screen is built on
    drifts away from what the business has actually committed to.

    Header and lines together or neither, which is why this is an
    operation and not two calls to the generic record handler. A
    header that survived a rejected line is the worst of the two
    outcomes: it reads as a placed order against quantities nobody
    agreed, and po_outstanding counts it as fully received because
    there is nothing under it to be outstanding.

    The currency rule is the receipt's rule, for the receipt's
    reason: 56 of something is not a price until it says 56 of what,
    at what. An order in the base currency needs no rate and gets 1.
    """
    org = _org(run)
    po_number = (po_number or "").strip().upper()
    if not po_number:
        raise NotAllowed("Give the order a number.")

    base = run.execute(
        "select currency from public.organisations where id = %s",
        (org,)).fetchone()[0]
    currency = (currency or "").strip().upper() or base
    if currency != base and fx is None:
        raise NotAllowed(
            f"This order is priced in {currency}, so it needs the rate "
            f"it was bought at. Without one there is no way to tell "
            f"{currency} from {base} further down.")
    rate = float(fx) if fx is not None else 1.0
    if rate <= 0:
        raise NotAllowed("A rate has to be more than nothing.")

    where = run.execute(
        "select id from public.locations where org_id = %s and upper(code)"
        " = %s", (org, (branch or "").strip().upper())).fetchone()
    if not where:
        raise NotAllowed(f"There is no branch {branch} here.")

    supplier_id = None
    supplier = (supplier or "").strip().upper()
    if supplier:
        found = run.execute(
            "select id from public.suppliers where org_id = %s"
            "   and upper(code) = %s", (org, supplier)).fetchone()
        if not found:
            raise NotAllowed(f"There is no supplier {supplier} here.")
        supplier_id = found[0]

    # Every line checked before anything is written. Walking them as
    # we insert would leave the first few in the database behind a
    # rejection of the fourth -- and this runs inside one transaction,
    # so it would roll back, but the error would name only the line
    # that failed and not the others the person also has to fix.
    if not lines:
        raise NotAllowed(
            "An order needs at least one line. A header with nothing "
            "under it reads as an order that has been fully received.")

    checked, problems = [], []
    for n, line in enumerate(lines, 1):
        sku = str(line.get("sku", "") or "").strip().upper()
        item = run.execute(
            "select id from public.items where org_id = %s"
            "   and upper(sku) = %s", (org, sku)).fetchone() if sku else None
        if not item:
            problems.append(f"Line {n}: there is no item {sku or '(blank)'}.")
            continue
        try:
            qty = float(line.get("qty"))
            price = float(line.get("unit_price"))
        except (TypeError, ValueError):
            problems.append(f"Line {n}: the quantity and the price have "
                            f"to be numbers.")
            continue
        if qty <= 0:
            problems.append(f"Line {n}: a quantity of {qty:g} is not an "
                            f"order for anything.")
            continue
        if price < 0:
            problems.append(f"Line {n}: a price cannot be negative.")
            continue
        if any(prior == item[0] for prior, _q, _p in checked):
            # The schema allows one line per item per order, and
            # 0010 says plainly that this index is what has to change
            # first if that ever stops being true. This is not that
            # moment: the import's idempotency rests on that natural
            # key, and repeatable re-import is worth more than two
            # prices for one item on one order -- which can be
            # expressed as two orders, or as one blended price.
            problems.append(
                f"Line {n}: {sku} is already on this order. Combine "
                f"the two lines, or raise a second order -- one item "
                f"can appear once per order.")
            continue
        checked.append((item[0], qty, price))

    if problems:
        raise NotAllowed(" ".join(problems))

    taken = run.execute(
        "select 1 from public.purchase_orders where org_id = %s"
        "   and upper(po_number) = %s", (org, po_number)).fetchone()
    if taken:
        raise NotAllowed(f"{po_number} is already an order here.")

    po = run.execute(
        "insert into public.purchase_orders (org_id, location_id,"
        " supplier_id, po_number, currency, fx_rate, status, ordered_at,"
        " expected_at, note, created_by)"
        # auth.uid(), not a value passed in: it is the signed-in user
        # on this connection, so it is right without being supplied
        # and cannot be supplied as somebody else. grn_lines and
        # shipment_orders default to the same expression; this column
        # has no default, so it is said here.
        " values (%s,%s,%s,%s,%s,%s,'placed',coalesce(%s, current_date),"
        "         %s,%s,auth.uid()) returning id",
        (org, where[0], supplier_id, po_number, currency, rate,
         ordered or None, expected or None,
         (note or "").strip() or None)).fetchone()[0]

    for item_id, qty, price in checked:
        run.execute(
            "insert into public.po_lines (org_id, po_id, item_id, qty,"
            " unit_price) values (%s,%s,%s,%s,%s)",
            (org, po, item_id, qty, price))

    return {"po": po_number, "lines": len(checked),
            "value": round(sum(q * p for _i, q, p in checked), 2),
            "currency": currency, "fx_rate": rate}


def take_payment(run, customer: str, amount: float, *,
                 received_on: str | None = None, currency: str = "",
                 fx: float | None = None, method: str = "",
                 reference: str = "", note: str = "",
                 allocate: Sequence[Mapping] = (),
                 client_ref: str | None = None) -> dict:
    """Money in, against the invoices it settles.

    Before this, the only record of a payment was the stored
    `amount_paid` on an invoice, moved by an import. So a collections
    call could not be answered from the system: when did they pay,
    how much, against what, and is the 400,000 that arrived on
    Tuesday the one they are asking about.

    Allocation is separate from the payment because a customer pays
    a round number against three invoices. What is left over sits on
    account, which is ordinary -- the cheque was for 500,000 and the
    invoices came to 400,000 -- and a book that cannot hold it
    either loses the 100,000 or puts it somewhere wrong.

    All of it or none. A payment that landed with half its
    allocations is a payment that will be recorded again by somebody
    who cannot tell.
    """
    org = _org(run)
    code = (customer or "").strip().upper()
    if not code:
        raise NotAllowed("Say which customer paid.")
    try:
        total = float(amount)
    except (TypeError, ValueError):
        raise NotAllowed("The amount has to be a number.") from None
    if total <= 0:
        raise NotAllowed(
            "A payment is money coming in. A refund is its own "
            "record, not a payment with a minus in front of it.")

    who = run.execute(
        "select id from public.customers where org_id = %s"
        "   and upper(code) = %s", (org, code)).fetchone()
    if not who:
        raise NotAllowed(f"There is no customer {code} here.")

    if client_ref:
        seen = run.execute(
            "select id from public.payments where org_id = %s"
            "   and client_ref = %s", (org, client_ref)).fetchone()
        if seen:
            # Already recorded. Answering as though it had just
            # worked is the point: the caller cannot tell, and a
            # refusal would send somebody looking for a payment that
            # is already there.
            return {"payment": str(seen[0]), "amount": total,
                    "allocated": 0.0, "on_account": 0.0,
                    "already": True}

    base = run.execute(
        "select currency from public.organisations where id = %s",
        (org,)).fetchone()[0]
    currency = (currency or "").strip().upper() or base
    if currency != base and fx is None:
        raise NotAllowed(
            f"This payment is in {currency}, so it needs the rate it "
            f"was converted at.")
    rate = float(fx) if fx is not None else 1.0

    # Every allocation checked before anything is written, so one
    # answer names everything the person has to fix rather than the
    # first thing only.
    checked, problems, running = [], [], 0.0
    for n, line in enumerate(allocate or (), 1):
        no = str(line.get("invoice", "") or "").strip().upper()
        try:
            part = float(line.get("amount"))
        except (TypeError, ValueError):
            problems.append(f"Line {n}: the amount has to be a number.")
            continue
        if part <= 0:
            problems.append(f"Line {n}: an allocation of nothing is not "
                            f"an allocation.")
            continue
        inv = run.execute(
            "select s.id, s.outstanding from public.invoice_settlement s"
            " where s.org_id = %s and upper(s.invoice_no) = %s"
            "   and s.customer_id = %s", (org, no, who[0])).fetchone()
        if not inv:
            problems.append(
                f"Line {n}: {code} has no invoice {no or '(blank)'}.")
            continue
        owing = float(inv[1])
        if part > owing + 1e-9:
            problems.append(
                f"Line {n}: {no} has {owing:,.0f} outstanding, so "
                f"{part:,.0f} cannot go against it. Overpaying one "
                f"invoice hides the credit owed back.")
            continue
        running += part
        checked.append((inv[0], part))

    if running > total + 1e-9:
        problems.append(
            f"That allocates {running:,.0f} of a {total:,.0f} payment, "
            f"which is more than came in.")
    if problems:
        raise NotAllowed(" ".join(problems))

    pay = run.execute(
        "insert into public.payments (org_id, customer_id, received_on,"
        " amount, currency, fx_rate, method, reference, note, client_ref)"
        " values (%s,%s,coalesce(%s, current_date),%s,%s,%s,%s,%s,%s,%s)"
        " returning id",
        (org, who[0], received_on or None, total, currency, rate,
         (method or "").strip().lower() or None,
         (reference or "").strip() or None,
         (note or "").strip() or None, client_ref)).fetchone()[0]

    for invoice_id, part in checked:
        run.execute(
            "insert into public.payment_allocations (org_id, payment_id,"
            " invoice_id, amount) values (%s,%s,%s,%s)",
            (org, pay, invoice_id, part))

    return {"payment": str(pay), "amount": total,
            "allocated": round(running, 2),
            "on_account": round(total - running, 2),
            "settled": len(checked)}
