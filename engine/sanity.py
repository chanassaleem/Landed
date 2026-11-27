"""Data sanity: the checks row-level parsing cannot make.

engine/parse.py validates a row against itself. It catches a negative
quantity, an unparseable date, a margin written as 35 where a decimal was
expected. What it cannot catch is everything that only shows up when the files
are looked at together, or against each other, or against what the rest of the
file looks like.

Those are the failures that actually hurt, because they do not produce an
error. They produce a dashboard that computes cleanly and is wrong:

  - A `machine_key` in the service file that exists in no machines file. The
    wastage is real and attached to nothing, so it never reaches a placement's
    economics.
  - The same hospital as "PIMS", "Public Hospital A" and "PUBLIC HOSP A".
    Group exposure splits three ways and the largest debtor in the group
    disappears into three manageable-looking balances.
  - A `unit_cost` of 5 where every comparable line is 5,000, because that
    branch exports in thousands. Stock value is understated by a factor of a
    thousand and the number still looks like a number.
  - An expiry date in the past. Already written off, still counted as cover.

Each finding names the rows so it can be fixed, carries a severity, and says
what it does to the numbers rather than just that it is odd. Nothing here
changes a figure: sanity reports, it does not repair. Silently correcting a
customer's data is how a product loses the right to be trusted.
"""

from __future__ import annotations

import difflib
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from engine.fmt import money, qty
from engine.schema import (
    Commitment,
    DemandPeriod,
    ItemSnapshot,
    MachinePlacement,
    ReceivableInvoice,
    ServiceJob,
)

# Severity, highest first. BLOCKS means a figure on the dashboard is wrong
# rather than merely uncertain.
BLOCKS, WARNS, NOTES = "blocks", "warns", "notes"
_ORDER = {BLOCKS: 0, WARNS: 1, NOTES: 2}

# How far a cost may sit from its peers before it looks like a scale error.
SCALE_FACTOR = 100.0
# Similarity above which two customer names are probably the same customer.
NAME_SIMILARITY = 0.82
# Lead time variability of exactly zero everywhere is a default, not a figure.
MIN_ROWS_FOR_STATS = 4


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    dataset: str
    title: str
    detail: str
    effect: str                  # what it does to the numbers
    fix: str
    rows: Tuple[str, ...] = ()   # identifiers, not line numbers
    count: int = 0


def _f(code, severity, dataset, title, detail, effect, fix, rows=(), count=0):
    rows = tuple(str(r) for r in rows)
    return Finding(
        code=code, severity=severity, dataset=dataset, title=title,
        detail=detail, effect=effect, fix=fix,
        rows=rows[:25], count=count or len(rows),
    )


def _norm(name: str) -> str:
    """Strip punctuation, case and common company noise for comparison."""
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(
        r"\b(pvt|private|ltd|limited|hospital|hosp|clinic|lab|laboratory|"
        r"centre|center|govt|government|the)\b",
        " ", s,
    )
    return re.sub(r"\s+", " ", s).strip()


# ----------------------------------------------------- referential checks


def _referential(
    items, demand, invoices, jobs, placements, out: List[Finding]
) -> None:
    item_keys = {(i.item_key) for i in items}
    loc_keys = {(i.entity_id, i.location_id) for i in items}

    # Demand for an item that is not in the stock file.
    if items and demand:
        orphan = sorted({d.item_key for d in demand} - item_keys)
        if orphan:
            out.append(_f(
                "demand_orphan", BLOCKS, "demand",
                "Demand history for items that are not in the stock file",
                f"{len(orphan)} item codes appear in demand_history.csv and in "
                f"no row of inventory.csv: {', '.join(orphan[:6])}"
                + (" and others" if len(orphan) > 6 else "") + ".",
                "That demand is ignored entirely. The items it belongs to get "
                "no reorder point, and the ones that are in stock look like "
                "they have less demand than they do.",
                "Usually a code that differs between two exports. Reconcile the "
                "item codes, or add the missing items to the stock file.",
                orphan,
            ))

        # Stock with no demand at all: reported as NO_HISTORY, which is right,
        # but worth saying out loud because it is often a join failure.
        silent = sorted(item_keys - {d.item_key for d in demand})
        if silent:
            out.append(_f(
                "stock_no_demand", WARNS, "inventory",
                "Stock held for items with no demand history",
                f"{len(silent)} items have stock and no demand rows: "
                f"{', '.join(silent[:6])}"
                + (" and others" if len(silent) > 6 else "") + ".",
                "These are classed as no-history and get no policy, so they "
                "are reported but never reordered. If it is a join failure "
                "rather than genuinely new stock, their cover is unknown.",
                "Check whether the demand export covers the same date range "
                "and the same item codes.",
                silent,
            ))

    # Demand for a branch that holds no stock.
    if items and demand:
        orphan_loc = sorted(
            {(d.entity_id, d.location_id) for d in demand} - loc_keys
        )
        if orphan_loc:
            out.append(_f(
                "demand_orphan_location", BLOCKS, "demand",
                "Demand recorded at branches with no stock file",
                "Demand exists for "
                + ", ".join(f"{e}/{l}" for e, l in orphan_loc[:6])
                + ", but those branches have no inventory rows.",
                "Policy is computed per branch, so this demand never meets a "
                "stock position and is silently dropped.",
                "Export inventory for those branches, or correct the branch "
                "codes so they match.",
                [f"{e}/{l}" for e, l in orphan_loc],
            ))

    # Service jobs pointing at machines that do not exist.
    if jobs and placements:
        machine_keys = {m.machine_key for m in placements}
        orphan_jobs = sorted({j.machine_key for j in jobs} - machine_keys)
        if orphan_jobs:
            lost = sum(
                max(0.0, j.parts_cost - j.parts_charged)
                for j in jobs if j.machine_key in set(orphan_jobs)
            )
            out.append(_f(
                "service_orphan", BLOCKS, "service",
                "Service jobs against machines that are not in the machines file",
                f"{len(orphan_jobs)} machine codes appear in service_jobs.csv "
                f"and in no row of machines.csv: {', '.join(orphan_jobs[:6])}"
                + (" and others" if len(orphan_jobs) > 6 else "") + ".",
                f"The wastage on these jobs is counted in the service total but "
                f"never reaches a placement's economics, so about "
                f"{money(lost)} of uncharged parts is attached to nothing.",
                "Reconcile the asset codes between the service system and the "
                "placement register.",
                orphan_jobs,
            ))

    # Placements against customers who owe nothing and never have.
    if placements and invoices:
        customers = {i.customer_key for i in invoices}
        orphan_cust = sorted({m.customer_key for m in placements} - customers)
        if orphan_cust:
            out.append(_f(
                "placement_orphan_customer", WARNS, "machines",
                "Placements at customers with no invoices",
                f"{len(orphan_cust)} customers hold placed instruments and "
                f"appear nowhere in receivables: {', '.join(orphan_cust[:6])}.",
                "An instrument placed free and recovered through reagent "
                "margin should be generating invoices. Either the customer code "
                "differs between the two exports, or that machine is not "
                "actually pulling reagent.",
                "Check the customer codes first; if they match, the placement "
                "is stalled and module 7 should be showing it.",
                orphan_cust,
            ))


# ------------------------------------------------------- master data drift


def _master_data(invoices, placements, items, out: List[Finding]) -> None:
    """One customer under several spellings, one item under several names.

    This is the failure that is hardest to see and does the most damage,
    because every individual number looks reasonable.
    """
    # Same code, different names: harmless but confusing.
    names_by_key: Dict[str, set] = defaultdict(set)
    for i in invoices:
        if i.customer_name:
            names_by_key[i.customer_key].add(i.customer_name.strip())
    inconsistent = {k: v for k, v in names_by_key.items() if len(v) > 1}
    if inconsistent:
        out.append(_f(
            "customer_name_variants", NOTES, "receivables",
            "One customer code carrying several different names",
            "; ".join(
                f"{k}: " + " / ".join(sorted(v)[:3])
                for k, v in list(inconsistent.items())[:4]
            ),
            "The totals are right, because the code is what is grouped on. "
            "Only the label is unstable.",
            "Pick one name per code so reports read consistently.",
            list(inconsistent),
        ))

    # Different codes, probably the same customer. This one is expensive.
    keys = sorted(names_by_key)
    normalised = {k: _norm(next(iter(names_by_key[k]))) for k in keys}
    exposure: Dict[str, float] = defaultdict(float)
    for i in invoices:
        if not i.is_intercompany:
            exposure[i.customer_key] += max(0.0, i.amount - i.amount_paid)

    pairs: List[Tuple[str, str, float]] = []
    for a_i, a in enumerate(keys):
        for b in keys[a_i + 1:]:
            na, nb = normalised[a], normalised[b]
            if not na or not nb:
                continue
            if na == nb:
                score = 1.0
            else:
                score = difflib.SequenceMatcher(None, na, nb).ratio()
            if score >= NAME_SIMILARITY:
                pairs.append((a, b, exposure[a] + exposure[b]))

    if pairs:
        pairs.sort(key=lambda p: -p[2])
        out.append(_f(
            "customer_possible_duplicate", BLOCKS, "receivables",
            "Different customer codes that look like the same customer",
            "; ".join(f"{a} and {b}" for a, b, _ in pairs[:5])
            + ". Compared on the names after stripping punctuation and words "
              "like hospital, clinic and private.",
            f"Group exposure is grouped on the code, so a single debtor split "
            f"across codes appears as several manageable balances instead of "
            f"one large one. Up to {money(pairs[0][2])} is involved in the "
            f"largest pair.",
            "Decide whether these are one customer. If they are, map them to a "
            "single code before trusting the exposure figures.",
            [f"{a}+{b}" for a, b, _ in pairs],
        ))

    # Same item code described differently at different branches.
    desc_by_item: Dict[str, set] = defaultdict(set)
    for i in items:
        if i.description:
            desc_by_item[i.item_key].add(i.description.strip())
    drifted = {k: v for k, v in desc_by_item.items() if len(v) > 1}
    if drifted:
        out.append(_f(
            "item_description_variants", NOTES, "inventory",
            "One item code described differently at different branches",
            "; ".join(
                f"{k}: " + " / ".join(sorted(v)[:2])
                for k, v in list(drifted.items())[:4]
            ),
            "Nothing is miscalculated. It is a sign the two branches maintain "
            "their item master separately, which is where code drift starts.",
            "Agree one description per code.",
            list(drifted),
        ))


# ---------------------------------------------------------- scale and units


def _scale(items, out: List[Finding]) -> None:
    """Costs that look like a units error rather than a price."""
    by_item: Dict[str, List[ItemSnapshot]] = defaultdict(list)
    for i in items:
        by_item[i.item_key].append(i)

    suspects: List[str] = []
    for key, rows in by_item.items():
        costs = [r.unit_cost for r in rows if r.unit_cost > 0]
        if len(costs) < 2:
            continue
        median = statistics.median(costs)
        if median <= 0:
            continue
        for r in rows:
            if r.unit_cost <= 0:
                continue
            ratio = r.unit_cost / median
            if ratio >= SCALE_FACTOR or ratio <= 1 / SCALE_FACTOR:
                suspects.append(f"{key} at {r.entity_id}/{r.location_id}")

    if suspects:
        out.append(_f(
            "unit_cost_scale", BLOCKS, "inventory",
            "Unit costs that differ from the same item elsewhere by a factor "
            "of a hundred or more",
            f"{len(suspects)} rows: {'; '.join(suspects[:5])}"
            + (" and others" if len(suspects) > 5 else "") + ".",
            "Almost always a branch exporting in thousands while another "
            "exports in units. Stock value, excess value and every order value "
            "at that branch are wrong by that factor, and they still look like "
            "plausible numbers.",
            "Confirm the unit of the cost column in each export and convert "
            "before uploading. The engine never guesses a unit.",
            suspects,
        ))

    # A zero cost makes an item invisible to every value-based ranking.
    free = [f"{i.item_key} at {i.entity_id}/{i.location_id}"
            for i in items if i.unit_cost <= 0]
    if free:
        out.append(_f(
            "unit_cost_zero", WARNS, "inventory",
            "Items with no unit cost",
            f"{len(free)} rows have a unit cost of zero: {'; '.join(free[:5])}"
            + (" and others" if len(free) > 5 else "") + ".",
            "These carry no stock value, so they cannot be ranked by ABC, "
            "cannot be ordered by value, and vanish from every cash figure. "
            "They are not excluded, they are weightless.",
            "Supply a landed cost, even an approximate one.",
            free,
        ))


def _policy_inputs(items, out: List[Finding]) -> None:
    """Inputs that are technically valid and quietly understate safety stock."""
    no_lead = [f"{i.item_key} at {i.entity_id}/{i.location_id}"
               for i in items if i.lead_time_days <= 0]
    if no_lead:
        out.append(_f(
            "lead_time_zero", WARNS, "inventory",
            "Items with a lead time of zero",
            f"{len(no_lead)} rows: {'; '.join(no_lead[:5])}"
            + (" and others" if len(no_lead) > 5 else "") + ".",
            "A zero lead time means the reorder point is safety stock alone, "
            "so these items will be ordered far too late. For an importer a "
            "real lead time is 45 to 110 days.",
            "Fill in the order-to-receipt time in calendar days.",
            no_lead,
        ))

    if len(items) >= MIN_ROWS_FOR_STATS:
        with_lead = [i for i in items if i.lead_time_days > 0]
        if with_lead and all(i.lead_time_days_std == 0 for i in with_lead):
            out.append(_f(
                "lead_time_std_all_zero", WARNS, "inventory",
                "Lead time variability is zero on every item",
                "No row in the stock file records any variation in lead time.",
                "Safety stock comes from the variability of demand AND of lead "
                "time. With variability set to zero the engine assumes every "
                "shipment arrives exactly on day 75, which for an importer is "
                "the one thing that never happens. Safety stock is understated "
                "across the whole book, and the more so on long lead times.",
                "Measure it from actual receipt dates against order dates. "
                "Even a rough figure beats zero.",
            ))


# --------------------------------------------------------------- dates


def _dates(items, demand, invoices, jobs, placements, as_of, out) -> None:
    expired = [f"{i.item_key} at {i.entity_id}/{i.location_id}"
               for i in items
               if i.expiry_date and i.expiry_date < as_of and i.on_hand > 0]
    if expired:
        out.append(_f(
            "expiry_in_the_past", BLOCKS, "inventory",
            "Stock on hand with an expiry date that has already passed",
            f"{len(expired)} rows: {'; '.join(expired[:5])}"
            + (" and others" if len(expired) > 5 else "") + ".",
            "This stock is a write-off and is still being counted as cover, so "
            "months of cover is overstated and the reorder these items need is "
            "not being raised.",
            "Write it off in the source system, or correct the date if the "
            "batch was replaced.",
            expired,
        ))

    future = [m.machine_key for m in placements if m.placed_date > as_of]
    if future:
        out.append(_f(
            "placed_in_the_future", WARNS, "machines",
            "Placements dated after the as-of date",
            f"{len(future)} machines: {', '.join(future[:6])}.",
            "Months elapsed comes out as zero, so payback cannot be computed "
            "and they are reported as too early to judge.",
            "Correct the placement date, or move the as-of date forward.",
            future,
        ))

    backwards = [i.invoice_no for i in invoices if i.due_date < i.invoice_date]
    if backwards:
        out.append(_f(
            "due_before_invoice", WARNS, "receivables",
            "Invoices due before they were raised",
            f"{len(backwards)} invoices: {', '.join(backwards[:6])}.",
            "The ageing is computed from the due date, so these look older "
            "than they are and inflate the overdue book.",
            "Check the terms on these invoices.",
            backwards,
        ))

    if demand:
        periods = sorted({d.period for d in demand})
        newest = periods[-1]
        try:
            y, m = (int(x) for x in newest.split("-"))
            last_day = date(y, m, 1)
            months_stale = (as_of.year - last_day.year) * 12 + (
                as_of.month - last_day.month
            )
        except (ValueError, IndexError):
            months_stale = 0
        if months_stale >= 2:
            out.append(_f(
                "demand_stale", WARNS, "demand",
                "The demand history stops well before the as-of date",
                f"The most recent month in the file is {newest}, which is "
                f"{months_stale} months before {as_of.isoformat()}.",
                "Every forecast, reorder point and months-of-cover figure is "
                "built on demand that is already that old. Nothing is wrong "
                "arithmetically; it is all answering a question about a "
                "different month.",
                "Re-export the demand history, or set the as-of date to match "
                "the data.",
            ))

        # Gaps inside the series: a missing month is read as a real zero.
        gaps: List[str] = []
        by_item: Dict[str, List[str]] = defaultdict(list)
        for d in demand:
            by_item[(d.item_key, d.entity_id, d.location_id)].append(d.period)
        span = set(periods)
        for key, got in by_item.items():
            missing = span - set(got)
            if missing and len(got) >= 2:
                lo, hi = min(got), max(got)
                inside = {p for p in missing if lo < p < hi}
                if inside:
                    gaps.append(f"{key[0]} at {key[1]}/{key[2]}")
        if gaps:
            out.append(_f(
                "demand_gaps", WARNS, "demand",
                "Items with months missing from the middle of their history",
                f"{len(gaps)} series have gaps: {'; '.join(gaps[:5])}"
                + (" and others" if len(gaps) > 5 else "") + ".",
                "A month that is absent is not the same as a month that was "
                "zero, but it is treated as neither: the series is just "
                "shorter. That changes the demand class, which changes which "
                "forecast method is used and which service metric applies.",
                "Export every month in the range, with zeros where there was "
                "no demand.",
                gaps,
            ))


# ------------------------------------------------------------ duplicates


def _duplicates(items, demand, invoices, jobs, placements, out) -> None:
    dup_items = [
        f"{k[0]} at {k[1]}/{k[2]}"
        for k, n in Counter(
            (i.item_key, i.entity_id, i.location_id) for i in items
        ).items() if n > 1
    ]
    if dup_items:
        out.append(_f(
            "inventory_duplicate_rows", BLOCKS, "inventory",
            "The same item appears more than once at the same branch",
            f"{len(dup_items)} duplicates: {'; '.join(dup_items[:5])}"
            + (" and others" if len(dup_items) > 5 else "") + ".",
            "Both rows are evaluated, so the item is reported twice and its "
            "stock value is double counted in every total.",
            "De-duplicate the export. If the branch really holds the item in "
            "two bins, sum them into one row.",
            dup_items,
        ))

    dup_periods = [
        f"{k[0]} {k[3]} at {k[1]}/{k[2]}"
        for k, n in Counter(
            (d.item_key, d.entity_id, d.location_id, d.period) for d in demand
        ).items() if n > 1
    ]
    if dup_periods:
        out.append(_f(
            "demand_duplicate_rows", BLOCKS, "demand",
            "The same item and month appears more than once",
            f"{len(dup_periods)} duplicates: {'; '.join(dup_periods[:5])}"
            + (" and others" if len(dup_periods) > 5 else "") + ".",
            "Classification counts periods, so a duplicated month makes demand "
            "look more frequent and less variable than it is. Items drift "
            "toward smooth and get the wrong forecast method.",
            "De-duplicate on item and month.",
            dup_periods,
        ))

    dup_inv = [k for k, n in Counter(
        (i.invoice_no, i.entity_id) for i in invoices
    ).items() if n > 1]
    if dup_inv:
        out.append(_f(
            "invoice_duplicate", BLOCKS, "receivables",
            "The same invoice number appears more than once in one entity",
            f"{len(dup_inv)} duplicates: "
            + ", ".join(str(k[0]) for k in dup_inv[:6]) + ".",
            "Every copy is added to the book, so the receivable, the ageing "
            "and the financing cost are all overstated.",
            "De-duplicate the receivables export.",
            [k[0] for k in dup_inv],
        ))

    dup_jobs = [k for k, n in Counter(j.job_no for j in jobs).items() if n > 1]
    if dup_jobs:
        out.append(_f(
            "job_duplicate", WARNS, "service",
            "The same job number appears more than once",
            f"{len(dup_jobs)} duplicates: {', '.join(dup_jobs[:6])}.",
            "Wastage and downtime are counted once per row, so a duplicated "
            "job overstates both.",
            "De-duplicate on job number.",
            dup_jobs,
        ))

    dup_mach = [k for k, n in Counter(
        m.machine_key for m in placements
    ).items() if n > 1]
    if dup_mach:
        out.append(_f(
            "machine_duplicate", BLOCKS, "machines",
            "The same machine appears more than once",
            f"{len(dup_mach)} duplicates: {', '.join(dup_mach[:6])}.",
            "Capital deployed is double counted, so recovery as a percentage "
            "of capital is understated across the whole fleet.",
            "De-duplicate on the asset code.",
            dup_mach,
        ))


# -------------------------------------------------------------- coverage


def _coverage(
    items, demand, invoices, jobs, placements, commitments, out
) -> None:
    supplied = {
        "inventory": bool(items), "demand": bool(demand),
        "receivables": bool(invoices), "service": bool(jobs),
        "machines": bool(placements), "commitments": bool(commitments),
    }
    missing = [k for k, v in supplied.items() if not v]

    if "commitments" in missing and (invoices or placements):
        out.append(_f(
            "no_commitments", WARNS, "commitments",
            "No committed payments were supplied",
            "commitments.csv is absent while receivables or placements are "
            "present, so a cash view has been built from inflows only.",
            "Letters of credit, bank repayments, payroll and rent go out "
            "whatever the stock position says. Without them the cash view is "
            "optimistic and the order headroom is overstated, which is the "
            "figure that decides what gets ordered.",
            "Export the committed payments for the next quarter.",
        ))

    if "demand" in missing and items:
        out.append(_f(
            "no_demand", BLOCKS, "demand",
            "No demand history was supplied",
            "inventory.csv is present and demand_history.csv is not.",
            "Without demand there is no reorder point, no classification and "
            "no cover. Every item reads as no-history and the stock position "
            "cannot be judged at all.",
            "Export at least twelve months of monthly issues per item.",
        ))

    # A branch holding nothing is usually a filtered export.
    by_loc: Dict[Tuple[str, str], float] = defaultdict(float)
    for i in items:
        by_loc[(i.entity_id, i.location_id)] += i.on_hand
    empty = [f"{e}/{l}" for (e, l), total in by_loc.items() if total <= 0]
    if empty:
        out.append(_f(
            "branch_empty", NOTES, "inventory",
            "Branches with rows but no stock at all",
            "No units on hand anywhere at " + ", ".join(empty[:6]) + ".",
            "Possible, but usually a filtered or partial export. Every item at "
            "that branch will read as needing an order.",
            "Check the export covered that branch's whole item list.",
            empty,
        ))


# ------------------------------------------------------------------ main


def _silently_incomplete(items, placements, out: List[Finding]) -> None:
    """Records that are present, and complete-looking, and are not.

    Every other check in this file looks for something absent, wrong
    or contradictory. These two look for a record where every field
    is reasonable and a downstream figure is quietly built on an
    absence -- which is the harder failure, because nothing on the
    screen is visibly wrong.

    Both were found by hand, after the figures they corrupt had been
    on screen for months.
    """
    # A placed machine with no reagent sale behind it. The Equipment
    # module cannot tell that from a customer who has stopped buying,
    # and the second reading is the one that sends somebody to a
    # hospital to retrieve a working analyser.
    unmeasured = [
        p.machine_key for p in placements
        if not getattr(p, "reagent_revenue_measured", False)
        and not p.reagent_revenue_to_date
    ]
    if unmeasured:
        capital = sum(
            p.capital_cost for p in placements
            if p.machine_key in set(unmeasured))
        out.append(_f(
            "placement_unmeasured", WARNS, "machines",
            "Placements with no reagent sale linked to them",
            f"{len(unmeasured)} of {len(placements)} placements, "
            f"holding {capital:,.0f} of capital.",
            "Recovery for these is unknown rather than zero, so the "
            "fleet percentage is across the rest. A machine nobody "
            "has linked a reagent to reads the same as one whose "
            "customer has stopped buying, and those need opposite "
            "responses.",
            "Link the reagents each model takes to its placement. The "
            "recovery then follows from sales already in the ledger; "
            "nothing has to be re-entered.",
            unmeasured,
        ))

    # A landed cost divided by a basis some lines on the shipment
    # were missing. The figure is the best available and worth
    # showing, but everything priced off it moves when the missing
    # weight is entered.
    provisional = [
        i.item_key for i in items
        if getattr(i, "cost_provisional", False)
    ]
    if provisional:
        out.append(_f(
            "cost_provisional", WARNS, "inventory",
            "Landed costs resting on a charge that could not be divided",
            f"{len(set(provisional))} item"
            f"{'s' if len(set(provisional)) != 1 else ''} across "
            f"{len(provisional)} line{'s' if len(provisional) != 1 else ''}.",
            "These unit costs will move once the missing weight or "
            "volume is entered, and the margin, the price floor, the "
            "ABC class and the stock valuation move with them.",
            "Enter the missing weight or volume on the Landing "
            "screen, which names the shipment and the figure it "
            "wants.",
            sorted(set(provisional)),
        ))


def check(
    items: Sequence[ItemSnapshot] = (),
    demand: Sequence[DemandPeriod] = (),
    invoices: Sequence[ReceivableInvoice] = (),
    jobs: Sequence[ServiceJob] = (),
    placements: Sequence[MachinePlacement] = (),
    commitments: Sequence[Commitment] = (),
    as_of: Optional[date] = None,
) -> Tuple[Finding, ...]:
    """Every cross-file check, worst first."""
    as_of = as_of or date.today()
    out: List[Finding] = []

    _referential(items, demand, invoices, jobs, placements, out)
    _master_data(invoices, placements, items, out)
    _scale(items, out)
    _policy_inputs(items, out)
    _dates(items, demand, invoices, jobs, placements, as_of, out)
    _duplicates(items, demand, invoices, jobs, placements, out)
    _coverage(items, demand, invoices, jobs, placements, commitments, out)
    _silently_incomplete(items, placements, out)

    out.sort(key=lambda f: (_ORDER[f.severity], -f.count, f.code))
    return tuple(out)


def summary(findings: Sequence[Finding]) -> dict:
    counts = Counter(f.severity for f in findings)
    return {
        "blocks": counts[BLOCKS],
        "warns": counts[WARNS],
        "notes": counts[NOTES],
        "total": len(findings),
        # Trustworthy means nothing is known to be wrong, not that the data is
        # right. The distinction matters and is worth keeping in the name.
        "nothing_known_wrong": counts[BLOCKS] == 0,
    }
