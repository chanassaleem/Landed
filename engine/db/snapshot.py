"""The tenant's own rows, in the shape the engine already understands.

The engine takes ItemSnapshot and DemandPeriod and knows nothing about
where they came from -- that is why it can be tested on its own, and
why a spreadsheet, a CSV and a Postgres schema can all feed the same
arithmetic. This module is the Postgres one.

Two things here are not obvious and both are deliberate.

The first is that every read goes through `as_user`. Not because the
query filters by organisation -- it does not need to, the policies do
that -- but because a query that ran as the owner would return every
tenant's rows while looking identical to this one. The scoping is the
connection, not the SQL.

The second is that a month with no demand has to arrive as a zero, not
as a missing row. To a forecast those are different facts: a zero is
'we sold none', a gap is 'we do not know'. The views return only months
that had movement, so the gaps are filled here, before the engine ever
sees them -- otherwise an item that sells in bursts reads as smooth,
its variability reads as nil, and its safety stock comes out at zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from engine.schema import DemandPeriod, ItemSnapshot

# How far back to read demand. Long enough for a seasonal pattern to be
# visible, short enough that a line's behaviour two years ago does not
# outvote what it has done since.
MONTHS = 18


def _months_back(as_of: date, n: int) -> list[str]:
    out, y, m = [], as_of.year, as_of.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


@dataclass(frozen=True)
class Snapshot:
    items: list[ItemSnapshot]
    demand: list[DemandPeriod]
    branches: list[str]
    currency: str

    @property
    def is_empty(self) -> bool:
        return not self.items


# An item appears at a branch when it has stock there or has been set
# up there -- not at every branch the organisation has. A cross join of
# every item against every location would invent rows for combinations
# the business has never used, and each one would arrive at the engine
# as a line with no demand and no stock, which it would dutifully
# classify and put on the screen.
ITEMS_SQL = """
select i.sku,
       i.description,
       i.category,
       l.code                                   as branch,
       coalesce(soh.on_hand, 0)                 as on_hand,
       coalesce(c.avg_unit_cost, 0)             as unit_cost,
       coalesce(il.lead_time_days, i.lead_time_days)         as lead_time_days,
       coalesce(il.lead_time_days_std, i.lead_time_days_std) as lead_time_std,
       i.is_clinical_critical,
       il.expiry_date
  from public.items i
  join public.locations l on l.org_id = i.org_id and l.is_active
  left join public.item_locations il
         on il.item_id = i.id and il.location_id = l.id
  left join public.stock_on_hand soh
         on soh.item_id = i.id and soh.location_id = l.id
  left join public.item_cost c
         on c.item_id = i.id
 where i.is_active
   and (il.item_id is not null or coalesce(soh.on_hand, 0) <> 0)
"""

DEMAND_SQL = """
select i.sku, l.code as branch, d.period, sum(d.quantity) as quantity
  from public.demand_monthly d
  join public.items i on i.id = d.item_id
  join public.locations l on l.id = d.location_id
 where d.period >= %s
 group by i.sku, l.code, d.period
"""

# What is ordered and not yet here. There is no "received" column on a
# PO line -- receipts are their own rows, which is what lets one order
# arrive in three shipments -- so the outstanding quantity is the
# ordered quantity less whatever the goods receipts account for.
OPEN_PO_SQL = """
with received as (
  select po_line_id, sum(qty) as qty
    from public.grn_lines
   where po_line_id is not null
   group by po_line_id
)
select i.sku, l.code as branch,
       sum(pl.qty - coalesce(r.qty, 0)) as on_order
  from public.po_lines pl
  join public.purchase_orders po on po.id = pl.po_id
  join public.items i on i.id = pl.item_id
  join public.locations l on l.id = po.location_id
  left join received r on r.po_line_id = pl.id
 where po.status in ('placed', 'part_received')
 group by i.sku, l.code
having sum(pl.qty - coalesce(r.qty, 0)) > 0
"""


def read(conn, as_of: date, currency: str = "PKR") -> Snapshot:
    """Build a snapshot from one tenant's rows.

    `conn` must already be scoped -- it comes from `as_user`, inside its
    transaction. There is no `where org_id = ...` anywhere here, and
    that is not an oversight: a redundant filter would make the policies
    look optional, and the day somebody removed one the query would keep
    working and start leaking.

    The items-to-locations join does carry org_id, and that one is a
    join key rather than a filter: a person can belong to two
    organisations, so the policies legitimately return both companies'
    rows on the same connection, and the two sets must not be paired
    with each other.

    Honestly, it is currently redundant -- a mutation test removing it
    changed no behaviour, because the WHERE below only keeps a row that
    has an item_locations entry or stock at that exact location, and
    neither can point across organisations. It stays because it says
    what the join means, costs nothing, and is what keeps that WHERE
    from being the only thing standing between two tenants if it is
    ever loosened. The test that pins this is in tests/test_snapshot.py
    and passes either way, which is the point: it pins the behaviour,
    not the clause that happens to produce it.
    """
    periods = _months_back(as_of, MONTHS)
    earliest = periods[0]

    on_order: dict[tuple[str, str], float] = {}
    for sku, branch, qty in conn.execute(OPEN_PO_SQL).fetchall():
        on_order[(sku, branch)] = float(qty or 0)

    items: list[ItemSnapshot] = []
    branches: set[str] = set()
    for row in conn.execute(ITEMS_SQL).fetchall():
        (sku, description, category, branch, on_hand, unit_cost,
         lead_time, lead_time_std, is_critical, expiry) = row
        branches.add(branch)
        items.append(ItemSnapshot(
            item_key=sku,
            description=description,
            category=category,
            on_hand=float(on_hand or 0),
            open_po=on_order.get((sku, branch), 0.0),
            unit_cost=float(unit_cost or 0),
            lead_time_days=float(lead_time or 0),
            lead_time_days_std=float(lead_time_std or 0),
            is_clinical_critical=bool(is_critical),
            expiry_date=expiry,
            location_id=branch,
        ))

    seen: dict[tuple[str, str], dict[str, float]] = {}
    for sku, branch, period, qty in conn.execute(
            DEMAND_SQL, (earliest,)).fetchall():
        seen.setdefault((sku, branch), {})[period] = float(qty or 0)

    # Every item gets every month, zeros included. See the note above:
    # a gap and a zero are different facts and only one of them is true.
    demand: list[DemandPeriod] = []
    for item in items:
        got = seen.get((item.item_key, item.location_id), {})
        for period in periods:
            demand.append(DemandPeriod(
                item_key=item.item_key,
                period=period,
                quantity=got.get(period, 0.0),
                location_id=item.location_id,
            ))

    return Snapshot(items=items, demand=demand,
                    branches=sorted(branches), currency=currency)
