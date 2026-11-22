"""The module registry: what this platform is made of.

Before this file the module list existed in three places that each knew
part of it -- a `MODULES` array in the browser with three entries, a
`ROLE_MODULES` map in the old auth module with twelve differently-named
ones, and a check constraint in migration 0001 with eleven more. Adding a
module meant editing all three and hoping they agreed. They did not: the
browser offered three modules while the database was prepared to license
eleven, and nothing reconciled them.

So the registry is here, once, and the rail, the API, the role gate and
the entitlement check all read it. A module that is not in this list does
not exist anywhere.

Each module declares what it NEEDS rather than what it shows. That
matters because the honest state of a module is a function of the data
present: a cash module with no bank balance and no commitments is not
"off", it is "waiting for figures", and telling the person which figures
is the difference between a product that looks broken and one that looks
unfinished on purpose.

`engine` names the function in engine/modules that does the arithmetic.
Where it is None the module is a record keeper rather than an analyser --
customers and contacts compute nothing, they hold things the analysers
read -- and saying so here keeps anyone from looking for the missing
mathematics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


# The records a module can depend on, named as the snapshot readers name
# them so a missing dependency can be reported without a translation
# table in between.
RECORDS = (
    "items", "demand", "invoices", "customers", "jobs", "placements",
    "commitments", "accounts", "employees", "opportunities",
    "marketing_spend", "shipments", "orders",
)


@dataclass(frozen=True)
class Module:
    """One module of the platform.

    `needs` is what the module cannot run without. `uses` is what makes
    it better but is not required: the cash view works from invoices
    alone and is sharper with commitments, and reporting that as a
    hard dependency would hide a working module behind a table nobody
    has filled yet.
    """

    id: str
    title: str
    # What the person gets, in their words, not the module's.
    blurb: str
    # The engine function, as "module.function", or None for a module
    # that keeps records rather than computing over them.
    engine: Optional[str]
    needs: tuple[str, ...] = ()
    uses: tuple[str, ...] = ()
    # Roles that may open it at all. Finer-grained field hiding is a
    # separate question and lives with the role, not with the module.
    roles: tuple[str, ...] = ("owner", "manager", "staff", "accountant")
    # Group-level modules mean nothing narrowed to one branch: a trial
    # balance belongs to a company and a transfer has two ends. The rail
    # uses this to stop offering a branch filter that would lie.
    group_level: bool = False
    # Where the module sits in the rail. Grouped so that the rail reads
    # as a business rather than as an alphabetical list of nouns.
    section: str = "operations"

    def ready(self, present: set[str]) -> bool:
        return all(r in present for r in self.needs)

    def missing(self, present: set[str]) -> tuple[str, ...]:
        return tuple(r for r in self.needs if r not in present)


# Order is the rail's order. Sections group them: what you hold, what
# money does, who you deal with, and what the engine concludes.
MODULES: tuple[Module, ...] = (
    Module(
        id="stock",
        title="What you hold",
        blurb="Every line at every branch, what it is worth, what runs "
              "out first, and what to move instead of buying.",
        engine="inventory.evaluate",
        needs=("items",),
        uses=("demand",),
        section="operations",
    ),
    Module(
        id="buy",
        title="What to order",
        blurb="What policy asks for, what the next thirteen weeks can "
              "fund, and which orders to place in what order.",
        engine="sequence.evaluate",
        needs=("items",),
        uses=("commitments", "invoices"),
        roles=("owner", "manager", "accountant"),
        section="operations",
    ),
    Module(
        id="freight",
        title="What it costs to land",
        blurb="Freight, duty and clearing spread across a shipment by "
              "value, weight or volume, so a price is the real one.",
        engine=None,
        needs=("shipments",),
        section="operations",
    ),
    Module(
        id="demand",
        title="What you will sell",
        blurb="A forecast per line per branch, by the method that "
              "line's demand pattern actually justifies.",
        engine="forecast.evaluate",
        needs=("items", "demand"),
        section="operations",
    ),
    Module(
        id="cash",
        title="What you can pay",
        blurb="Thirteen weeks of money in and out, timed by how your "
              "customers actually pay rather than when they promised.",
        engine="cash.evaluate",
        needs=("invoices",),
        uses=("commitments", "placements", "jobs"),
        roles=("owner", "manager", "accountant"),
        group_level=True,
        section="money",
    ),
    Module(
        id="money",
        title="What you are owed",
        blurb="The ageing book, each customer's real exposure across "
              "the group, and what the delay is costing in interest.",
        engine="receivables.evaluate",
        needs=("invoices",),
        roles=("owner", "manager", "accountant"),
        group_level=True,
        section="money",
    ),
    Module(
        id="sell",
        title="What you sold",
        blurb="Invoices raised, against true landed cost, so the "
              "margin on the line is the margin you kept.",
        engine=None,
        needs=("invoices",),
        uses=("items",),
        section="money",
    ),
    Module(
        id="books",
        title="Your statements",
        blurb="Profit, balance sheet and a cash flow derived from both "
              "rather than typed in, with the three checks that tie.",
        engine="finance.evaluate",
        needs=("accounts",),
        roles=("owner", "accountant"),
        group_level=True,
        section="money",
    ),
    Module(
        id="customers",
        title="Who you deal with",
        blurb="Customers and suppliers, their contacts, their terms "
              "and their credit limit, and every dealing recorded.",
        engine=None,
        needs=("customers",),
        section="people",
    ),
    Module(
        id="people",
        title="Who you employ",
        blurb="Payroll with the statutory deductions worked out at the "
              "rates you set, never at rates assumed for you.",
        engine="payroll.evaluate",
        needs=("employees",),
        roles=("owner", "accountant"),
        group_level=True,
        section="people",
    ),
    Module(
        id="equipment",
        title="What you placed",
        blurb="Instruments placed with a customer, the capital in "
              "each, and whether it pays back inside its contract.",
        engine="machines.evaluate",
        needs=("placements",),
        section="service",
    ),
    Module(
        id="service",
        title="What you serviced",
        blurb="Jobs, the hours and parts that went in against what "
              "was billed, and the repeat visits nobody counts.",
        engine="service.evaluate",
        needs=("jobs",),
        section="service",
    ),
    Module(
        id="intelligence",
        title="What to do next",
        blurb="Tenders and accounts being worked, weighted by odds "
              "actually observed, checked against stock and cash.",
        engine="marketing.evaluate",
        needs=("opportunities",),
        uses=("items", "invoices", "commitments"),
        roles=("owner", "manager"),
        group_level=True,
        section="decide",
    ),
)

SECTIONS: tuple[tuple[str, str], ...] = (
    ("operations", "Goods"),
    ("money", "Money"),
    ("people", "People"),
    ("service", "Service"),
    ("decide", "Decide"),
)

BY_ID: dict[str, Module] = {m.id: m for m in MODULES}


def get(module_id: str) -> Module:
    try:
        return BY_ID[module_id]
    except KeyError:
        raise KeyError(f"No module called {module_id!r}.") from None


def for_role(role: str) -> tuple[Module, ...]:
    """Modules this role may open.

    An unknown role gets nothing rather than everything. A typo in a
    membership row should lock someone out visibly, not hand them the
    accounts.
    """
    return tuple(m for m in MODULES if role in m.roles)


def catalogue(present: set[str] | None = None,
              role: str = "owner",
              licensed: set[str] | None = None) -> list[dict]:
    """The rail's content: every module, with its honest state.

    Four states, and the distinction between the middle two is the
    point. `ready` has what it needs. `waiting` is licensed and
    permitted but has no figures yet, and says which. `locked` is a
    module this organisation has not licensed. `hidden` never reaches
    the browser at all, because a role that may not open the books
    should not be told the books exist.
    """
    present = present if present is not None else set()
    allowed = {m.id for m in for_role(role)}
    out: list[dict] = []
    for m in MODULES:
        if m.id not in allowed:
            continue
        if licensed is not None and m.id not in licensed:
            state = "locked"
        elif m.ready(present):
            state = "ready"
        else:
            state = "waiting"
        out.append({
            "id": m.id,
            "title": m.title,
            "blurb": m.blurb,
            "engine": m.engine,
            "section": m.section,
            "group_level": m.group_level,
            "state": state,
            "missing": list(m.missing(present)),
            "needs": list(m.needs),
            "uses": list(m.uses),
        })
    return out
