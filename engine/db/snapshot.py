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


# ===================================================== selling and service
#
# The same job as above for the other three modules: take one tenant's
# rows and hand back exactly the shapes engine/modules/receivables.py,
# machines.py and service.py already take. Those modules have 47 tests
# between them and none of them knows where a row came from, which is
# why a Postgres schema can feed the same arithmetic a spreadsheet did.

INVOICES_SQL = """
select i.invoice_no, c.code, c.name, i.invoice_date, i.due_date,
       i.amount, i.amount_paid, c.is_public_sector, c.is_intercompany,
       coalesce(l.code, 'default')
  from public.invoices i
  join public.customers c on c.id = i.customer_id
  left join public.locations l on l.id = i.location_id
"""

MACHINES_SQL = """
select m.machine_key, m.model, coalesce(c.code, ''), coalesce(c.name, ''),
       m.placed_date, m.capital_cost, m.contract_months,
       m.expected_monthly_tests, m.revenue_per_test, m.reagent_margin_pct,
       coalesce(l.code, 'default'), m.id
  from public.machines m
  left join public.customers c on c.id = m.customer_id
  left join public.locations l on l.id = m.location_id
 where m.status = 'placed'
"""

JOBS_SQL = """
select j.job_no, coalesce(m.machine_key, ''), j.opened_date, j.closed_date,
       j.labour_hours, j.labour_hours_billed, j.parts_cost, j.parts_charged,
       j.is_warranty, j.is_repeat_visit, j.downtime_days,
       coalesce(l.code, 'default'), j.machine_id
  from public.service_jobs j
  left join public.machines m on m.id = j.machine_id
  left join public.locations l on l.id = j.location_id
 where j.opened_date >= %s
"""

# Reagent pull per machine, which is what decides whether a placement
# has paid for itself. Taken from the ledger rather than stored on the
# machine: a stored total is a second number that drifts.
MACHINE_PULL_SQL = """
select j.machine_id, count(*) filter (where true) as jobs
  from public.service_jobs j
 where j.machine_id is not null
 group by j.machine_id
"""


def read_invoices(conn):
    from engine.schema import ReceivableInvoice

    return [
        ReceivableInvoice(
            invoice_no=r[0], customer_key=r[1], customer_name=r[2],
            invoice_date=r[3], due_date=r[4],
            amount=float(r[5]), amount_paid=float(r[6]),
            is_public_sector=bool(r[7]), is_intercompany=bool(r[8]),
            location_id=r[9],
        )
        for r in conn.execute(INVOICES_SQL).fetchall()
    ]


def read_machines(conn):
    """Placed machines, with what they have actually pulled.

    reagent_revenue_to_date and the test counts come from the ledger
    and the service log rather than from columns on the machine. A
    stored total is a second number that drifts from the movements
    behind it, and this is the figure the whole record exists for.
    """
    from engine.schema import MachinePlacement

    out = []
    for r in conn.execute(MACHINES_SQL).fetchall():
        (key, model, ckey, cname, placed, capital, months, expected,
         per_test, margin, loc, mid) = r
        spend = conn.execute(
            "select coalesce(sum(j.parts_cost), 0), "
            "       coalesce(sum(j.downtime_days), 0) "
            "  from public.service_jobs j where j.machine_id = %s", (mid,)
        ).fetchone()
        months_run = 0.0
        if placed:
            months_run = max(
                0.0, (date.today() - placed).days / 30.44)
        actual = float(expected or 0)       # until meter readings are in
        revenue = actual * months_run * float(per_test or 0)
        out.append(MachinePlacement(
            machine_key=key, model=model, customer_key=ckey or key,
            customer_name=cname or "(unassigned)",
            placed_date=placed or date.today(),
            capital_cost=float(capital or 0),
            contract_months=int(months or 36),
            reagent_revenue_to_date=revenue,
            reagent_margin_pct=float(margin or 0),
            expected_monthly_tests=float(expected or 0),
            actual_monthly_tests=actual,
            downtime_days_ytd=float(spend[1] or 0),
            service_cost_to_date=float(spend[0] or 0),
            revenue_per_test=float(per_test or 0),
            location_id=loc,
        ))
    return out


def read_jobs(conn, since: date | None = None):
    from engine.schema import ServiceJob

    since = since or date(date.today().year - 1, 1, 1)
    return [
        ServiceJob(
            job_no=r[0], machine_key=r[1], opened_date=r[2], closed_date=r[3],
            labour_hours=float(r[4]), labour_hours_billed=float(r[5]),
            parts_cost=float(r[6]), parts_charged=float(r[7]),
            is_warranty=bool(r[8]), is_repeat_visit=bool(r[9]),
            downtime_days=float(r[10]), location_id=r[11],
        )
        for r in conn.execute(JOBS_SQL, (since,)).fetchall()
    ]


# ------------------------------------------------------------ migration 0009
#
# Readers for the four modules that were written, tested, and had
# nowhere to read from: the statements, the commitments half of the cash
# view, payroll and the pipeline. Each one returns the dataclass its
# module already takes, so no module changed to gain a database.
#
# entity_id is left at its default throughout, as the readers above do.
# A connection is scoped to one organisation by the policies, so every
# row a read returns belongs to the same entity and a column saying so
# would be a constant. When an organisation can hold several legal
# entities that changes, and it changes here rather than in the engine.


def read_accounts(conn, period_to: date | None = None):
    """The trial balance for the latest period at or before a date.

    `period_to` picks the period rather than filtering by it: a trial
    balance is as at one date, and summing two periods' worth of
    closing balances produces a statement that balances by accident or
    not at all. Where several periods are loaded the most recent one
    that has closed on or before the date wins, which is what somebody
    asking for "the accounts" in March means.
    """
    from engine.modules.finance import Account

    period_to = period_to or date.today()
    row = conn.execute(
        "select max(period_to) from public.accounts where period_to <= %s",
        (period_to,),
    ).fetchone()
    if not row or row[0] is None:
        return []
    chosen = row[0]
    return [
        Account(
            entity_id="default",
            code=r[0], name=r[1], line=r[2],
            opening=float(r[3] or 0), closing=float(r[4] or 0),
        )
        for r in conn.execute(
            "select code, name, line, opening, closing"
            "  from public.accounts where period_to = %s"
            " order by code",
            (chosen,),
        ).fetchall()
    ]


def accounts_period(conn, period_to: date | None = None):
    """Which period `read_accounts` would return, so a screen can say so.

    A statement shown without the period it covers is a number nobody
    can check.
    """
    period_to = period_to or date.today()
    row = conn.execute(
        "select period_from, period_to from public.accounts"
        " where period_to = (select max(period_to) from public.accounts"
        "                     where period_to <= %s)"
        " limit 1",
        (period_to,),
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def read_dividends(conn, period_from: date, period_to: date) -> float:
    """Dividends paid in the period, for the retained-earnings check.

    Without this the check fails for every company that pays one:
    closing retained earnings less opening does not equal profit when a
    dividend went out in between, and the module would report a correct
    set of books as broken.
    """
    row = conn.execute(
        "select coalesce(sum(amount), 0) from public.equity_movements"
        " where kind = 'dividend' and moved_on between %s and %s",
        (period_from, period_to),
    ).fetchone()
    return float(row[0] or 0)


def read_commitments(conn, as_of: date | None = None):
    """Outflows already committed, unsettled ones only.

    A settled commitment has already left the account, so counting it
    again in the horizon charges the same money twice. The recurrence is
    passed through rather than expanded: engine/modules/cash.py has
    `expand_commitments` for that, and expanding here would mean two
    places that know how a recurrence works.
    """
    from engine.schema import Commitment, CommitmentKind

    as_of = as_of or date.today()
    return [
        Commitment(
            label=r[0],
            kind=CommitmentKind(r[1]),
            due_date=r[2],
            amount=float(r[3] or 0),
            is_recurring=bool(r[4]),
            recurs_every_days=int(r[5] or 0),
            location_id=str(r[6]) if r[6] else "default",
        )
        for r in conn.execute(
            "select label, kind, due_date, amount, is_recurring,"
            "       recurs_every_days, location_id"
            "  from public.commitments"
            " where is_settled = false"
            "   and (due_date >= %s or is_recurring)"
            " order by due_date",
            (as_of,),
        ).fetchall()
    ]


def read_cash_balance(conn, as_of: date | None = None):
    """The opening bank balance, as at the latest date not after `as_of`.

    Returns None rather than zero when nothing has been entered. Zero
    is a balance, and a cash view that opens at zero because nobody
    filled the figure in reports a business about to miss payroll.
    """
    as_of = as_of or date.today()
    row = conn.execute(
        "select sum(balance), max(as_at) from public.cash_balances"
        " where as_at = (select max(as_at) from public.cash_balances"
        "                 where as_at <= %s)",
        (as_of,),
    ).fetchone()
    if not row or row[0] is None:
        return None, None
    return float(row[0]), row[1]


def read_employees(conn):
    from engine.payroll import Employee

    return [
        Employee(
            employee_id=r[0], name=r[1], entity_id="default",
            location_id=str(r[2]) if r[2] else "default",
            department=r[3] or "",
            basic_monthly=float(r[4] or 0),
            allowances_monthly=float(r[5] or 0),
            medical_monthly=float(r[6] or 0),
            employer_pf_rate=float(r[7] or 0),
            employee_pf_rate=float(r[8] or 0),
            joined=r[9], left=r[10],
            eobi_registered=bool(r[11]),
            social_security=bool(r[12]),
            is_active=bool(r[13]),
        )
        for r in conn.execute(
            "select employee_no, name, location_id, department,"
            "       basic_monthly, allowances_monthly, medical_monthly,"
            "       employer_pf_rate, employee_pf_rate, joined, left_on,"
            "       eobi_registered, social_security, is_active"
            "  from public.employees order by name"
        ).fetchall()
    ]


def read_opportunities(conn):
    """The pipeline, with the item each opportunity would consume.

    The item key is joined in rather than held on the row because
    engine/modules/marketing.py checks the pipeline against the stock
    position by item key, and a stored copy of the key drifts the first
    time somebody renames a code.
    """
    from engine.modules.marketing import Opportunity

    return [
        Opportunity(
            opportunity_id=r[0],
            customer_key=r[1] or "",
            customer_name=r[2] or "(none)",
            entity_id="default",
            location_id=str(r[3]) if r[3] else "default",
            stage=r[4],
            value=float(r[5] or 0),
            expected_close=r[6],
            item_key=r[7] or "",
            quantity=float(r[8] or 0),
            source=r[9] or "",
            owner=r[10] or "",
            opened=r[11],
            closed=r[12],
        )
        for r in conn.execute(
            "select o.reference, c.code, c.name, o.location_id, o.stage,"
            "       o.value, o.expected_close, i.sku, o.quantity,"
            "       o.source, o.owner_name, o.opened, o.closed"
            "  from public.opportunities o"
            "  left join public.customers c on c.id = o.customer_id"
            "  left join public.items i on i.id = o.item_id"
            " order by o.expected_close nulls last, o.value desc"
        ).fetchall()
    ]


def read_marketing_spend(conn, since: date | None = None):
    from engine.modules.marketing import Spend

    since = since or date(date.today().year - 1, 1, 1)
    return [
        Spend(entity_id="default", label=r[0], category=r[1],
              spent_on=r[2], amount=float(r[3] or 0))
        for r in conn.execute(
            "select label, category, spent_on, amount"
            "  from public.marketing_spend where spent_on >= %s"
            " order by spent_on desc",
            (since,),
        ).fetchall()
    ]


# What record types this organisation actually holds. The module
# registry turns this into each module's state, which is why it counts
# rows rather than checking that a table exists: every table exists in
# every organisation from the moment the migration runs, and a module
# whose table is empty is waiting for figures, not ready.
PRESENCE_SQL: dict[str, str] = {
    "items": "select 1 from public.items limit 1",
    "demand": "select 1 from public.demand_history limit 1",
    "invoices": "select 1 from public.invoices limit 1",
    "customers": "select 1 from public.customers limit 1",
    "jobs": "select 1 from public.service_jobs limit 1",
    "placements": "select 1 from public.machines limit 1",
    "commitments": "select 1 from public.commitments where is_settled = false limit 1",
    "accounts": "select 1 from public.accounts limit 1",
    "employees": "select 1 from public.employees where is_active limit 1",
    "opportunities": "select 1 from public.opportunities limit 1",
    "marketing_spend": "select 1 from public.marketing_spend limit 1",
    "shipments": "select 1 from public.shipments limit 1",
    "orders": "select 1 from public.purchase_orders limit 1",
}


def present(conn) -> set[str]:
    """Which record types have at least one row this user can see.

    One statement rather than thirteen round trips, because this runs
    on every page load and the modules screen is the first thing anyone
    sees. Each arm is a `limit 1` existence check, so none of them
    reads a table.

    A record type whose query fails is reported absent rather than
    raising. The policies refuse a table the role may not read, and a
    manager who cannot see payroll should be told the payroll module is
    waiting for figures, not handed a 500.
    """
    arms = ", ".join(
        f"exists({sql}) as {name}" for name, sql in PRESENCE_SQL.items()
    )
    try:
        row = conn.execute(f"select {arms}").fetchone()
    except Exception:
        out = set()
        for name, sql in PRESENCE_SQL.items():
            try:
                if conn.execute(f"select exists({sql})").fetchone()[0]:
                    out.add(name)
            except Exception:
                continue
        return out
    return {
        name for name, got in zip(PRESENCE_SQL, row) if got
    }
