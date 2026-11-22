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
    # One word, for the rail and the heading. A sentence reads well the
    # first time and scans badly every time after: somebody looking for
    # the receivables does not want to read "What you are owed" among
    # thirteen other sentences, they want to find the word and click
    # it. The sentence is not thrown away -- `title` is what the hover
    # card says, so the explanation is still there for whoever has not
    # met the word before.
    name: str
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
    # What somebody actually does here, in order. Not a feature list:
    # the steps a person works through, so that a screen full of
    # figures has a stated first move rather than leaving everyone to
    # work out for themselves where to start. Most software of this
    # kind is bought by one person and used by another, and the second
    # person is the one who needs this.
    process: tuple[str, ...] = ()
    # Why the module is worth the time, in terms of the money it moves
    # rather than the feature it is. One paragraph, specific to this
    # trade: an importer's reasons are not a retailer's.
    why: str = ""
    # Why a group-level module cannot be narrowed to one branch. The
    # browser carried a single sentence for all six -- "a statement
    # belongs to a company, one bank balance serves the lot, and a
    # transfer has two ends" -- which meant that on any one of them at
    # least two of the three reasons were about some other screen. The
    # data-quality module was explaining itself in terms of bank
    # balances.
    whole_business: str = ""

    def ready(self, present: set[str]) -> bool:
        return all(r in present for r in self.needs)

    def missing(self, present: set[str]) -> tuple[str, ...]:
        return tuple(r for r in self.needs if r not in present)


# Order is the rail's order. Sections group them: what you hold, what
# money does, who you deal with, and what the engine concludes.
MODULES: tuple[Module, ...] = (
    Module(
        id="stock",
        name="Stock",
        title="What you hold",
        blurb="Every line at every branch, what it is worth, what runs "
              "out first, and what to move instead of buying.",
        engine="inventory.evaluate",
        needs=("items",),
        uses=("demand",),
        section="operations",
        process=(
            "Pick the branch, or leave it on all of them.",
            "Work the lines that need a decision first — the view is there for it.",
            "For anything running out, check whether another branch is holding it before raising an order.",
            "Open a line to see its movements, and record a receipt, a sale or a count against it.",
            "Correct a lead time, a price or a description in the cell itself.",
        ),
        why=
             "Stock is where an importer's money sits still. A line that is "
             "standing still at one branch and running out at another is a "
             "cost twice over: the holding, and the import that was not "
             "needed. This is the only screen that sees both ends at once, "
             "which is why the transfer suggestion is here and not in "
             "Ordering.",
    ),
    Module(
        id="buy",
        name="Ordering",
        title="What to order",
        blurb="What policy asks for, what the next thirteen weeks can "
              "fund, and which orders to place in what order.",
        engine="sequence.evaluate",
        needs=("items",),
        uses=("commitments", "invoices"),
        roles=("owner", "manager", "accountant"),
        section="operations",
        process=(
            "Start with what policy asks for — the quantity is already worked out from demand, lead time and the service target.",
            "Compare it against what the next thirteen weeks can fund.",
            "Work down in the order given: what is short soonest, weighed against what the money allows.",
            "Place the orders you have decided on, then come back when the cash position changes.",
        ),
        why=
             "An importer orders months before the money comes back, so the "
             "constraint is rarely the warehouse — it is the cash and the "
             "letter of credit. Ordering everything policy asks for at once "
             "is how a profitable business runs out of money. The order of "
             "the list is the answer to that, not the list itself.",
    ),
    Module(
        id="freight",
        name="Landing",
        title="What it costs to land",
        blurb="Freight, duty and clearing spread across a shipment by "
              "value, weight or volume, so a price is the real one.",
        engine=None,
        needs=("shipments",),
        section="operations",
        process=(
            "Enter the shipment and its lines.",
            "Add each cost against it — freight, duty, clearing, inland — as it is invoiced.",
            "Choose how each cost is spread: by value, by weight, by volume or by quantity.",
            "Check the landed unit cost that comes out, and fix the basis if a line looks wrong.",
        ),
        why=
             "The invoice price is not the cost. Freight, duty and clearing "
             "add a fifth to a half in this trade, and a margin worked out "
             "against the invoice overstates itself by exactly that. Every "
             "cost figure in the rest of the platform comes from here, which "
             "is why getting the basis right matters more than getting it "
             "quickly.",
    ),
    Module(
        id="demand",
        name="Forecast",
        title="What you will sell",
        blurb="A forecast per line per branch, by the method that "
              "line's demand pattern actually justifies.",
        engine="forecast.evaluate",
        needs=("items", "demand"),
        section="operations",
        process=(
            "Check how each line is classified — steady, lumpy, intermittent — because the method follows the pattern.",
            "Read the forecast and the spread around it, not just the number.",
            "Look at the lines the engine says it cannot forecast well, and decide them by hand.",
            "Feed what you decide back into the cover target in Settings.",
        ),
        why=
             "Everything downstream is a bet on this. A reorder point is a "
             "forecast plus a safety margin, and a safety margin is a "
             "statement about how wrong the forecast might be. A line that "
             "sells four times a year does not have an average worth using, "
             "and treating it as if it did is how an importer ends up "
             "holding three years of a reagent nobody ordered.",
    ),
    Module(
        id="cash",
        name="Cash",
        title="What you can pay",
        blurb="Thirteen weeks of money in and out, timed by how your "
              "customers actually pay rather than when they promised.",
        engine="cash.evaluate",
        needs=("invoices",),
        uses=("commitments", "placements", "jobs"),
        roles=("owner", "manager", "accountant"),
        group_level=True,
        section="money",
        process=(
            "Enter the bank balance and the commitments you already know about.",
            "Read the thirteen weeks forward and find the first week that dips.",
            "Check what is in that week — each one says what it is made of.",
            "Move what can be moved, and take the rest back to Ordering.",
        ),
        why=
             "A letter of credit falls due whether or not the stock has "
             "sold. This is the constraint that Ordering reads, so a week "
             "that dips here is an order that does not get placed there. "
             "Seeing it eleven weeks out is the difference between "
             "rescheduling a payment and asking for one.",
        whole_business=
                 "One bank balance serves the whole business, so a Karachi "
                 "cash position would be a number nobody holds.",
    ),
    Module(
        id="money",
        name="Owed",
        title="What you are owed",
        blurb="The ageing book, each customer's real exposure across "
              "the group, and what the delay is costing in interest.",
        engine="receivables.evaluate",
        needs=("invoices",),
        roles=("owner", "manager", "accountant"),
        group_level=True,
        section="money",
        process=(
            "Work the oldest first, then the largest.",
            "Check which customers are consistently late, not just currently late.",
            "Chase, and record what was said.",
            "Take anything genuinely uncollectable out, so the figure means something.",
        ),
        why=
             "For an importer this is usually larger than the stock. Money "
             "already spent on goods already delivered, sitting with "
             "somebody else, is the cheapest money in the business to get "
             "back — no import, no freight, no duty, no wait. It is also the "
             "figure most often wrong, because nobody enjoys the job.",
        whole_business=
                 "An ageing book is read against one credit limit per "
                 "customer, and a customer buys from more than one branch.",
    ),
    Module(
        id="sell",
        name="Sales",
        title="What you sold",
        blurb="Invoices raised, against true landed cost, so the "
              "margin on the line is the margin you kept.",
        engine=None,
        needs=("invoices",),
        uses=("items",),
        section="money",
        process=(
            "Read what actually sold, by line and by branch.",
            "Check margin against the landed cost, not the invoice price.",
            "Find the lines that sell well and earn nothing.",
            "Take what you learn to pricing, or to Ordering.",
        ),
        why=
             "Revenue is the easy number and the least useful one. The "
             "question is which lines pay for themselves once freight, duty "
             "and clearing are in, and in this trade that reorders the list "
             "more often than people expect — a heavy, low-value line can be "
             "sold at a loss for years without anyone noticing.",
    ),
    Module(
        id="books",
        name="Books",
        title="Your statements",
        blurb="Profit, balance sheet and a cash flow derived from both "
              "rather than typed in, with the three checks that tie.",
        engine="finance.evaluate",
        needs=("accounts",),
        roles=("owner", "accountant"),
        group_level=True,
        section="money",
        process=(
            "Check the three that have to tie: the trial balance, the stock, and the movements behind it.",
            "Read the profit and loss, the balance sheet and the trial\n             balance for the period.",
            "Find anything that does not reconcile and follow it back to the record that caused it.",
        ),
        why=
             "Everything else in the platform is an opinion about the "
             "future. This is the part that has to agree with the bank and "
             "the auditor. If the stock figure here and the stock figure on "
             "the Stock screen differ, one of them is wrong, and this is the "
             "screen that tells you.",
        whole_business=
                 "A statement belongs to a company. There is no Lahore "
                 "balance sheet unless somebody keeps one.",
    ),
    Module(
        id="customers",
        name="Customers",
        title="Who you deal with",
        blurb="Customers and suppliers, their contacts, their terms "
              "and their credit limit, and every dealing recorded.",
        engine=None,
        needs=("customers",),
        section="people",
        process=(
            "Keep who you deal with, where, and on what terms.",
            "Record the terms you actually agreed, which are not always the terms on the invoice.",
            "Check who is slow before extending more credit.",
        ),
        why=
             "A distributor's customers are hospitals and labs, which pay on "
             "their own schedule regardless of the terms. Knowing which ones "
             "before the order is placed, rather than after the invoice is "
             "ninety days old, is most of credit control.",
    ),
    Module(
        id="people",
        name="Payroll",
        title="Who you employ",
        blurb="Payroll with the statutory deductions worked out at the "
              "rates you set, never at rates assumed for you.",
        engine="payroll.evaluate",
        needs=("employees",),
        roles=("owner", "accountant"),
        group_level=True,
        section="people",
        process=(
            "Keep who you employ, where, and on what.",
            "Check the rates against the current Finance Act before a run.",
            "Run the month, and read what it does to the cash weeks.",
        ),
        why=
             "Payroll is the commitment that does not reschedule. In the "
             "cash view it is the floor under every other decision: an order "
             "that can only be funded by being late on salaries is not a "
             "funded order.",
        whole_business=
                 "Payroll is run and paid once, and the rates are set by the "
                 "province rather than by the branch.",
    ),
    Module(
        id="equipment",
        name="Equipment",
        title="What you placed",
        blurb="Instruments placed with a customer, the capital in "
              "each, and whether it pays back inside its contract.",
        engine="machines.evaluate",
        needs=("placements",),
        section="service",
        process=(
            "Record each machine placed, where, and with whom.",
            "Link the consumables that run on it.",
            "Watch what each placement actually consumes against what it was expected to.",
        ),
        why=
             "Placing an analyser is lending capital to a customer in "
             "exchange for a consumables stream. Whether that was a good "
             "trade depends entirely on the consumption, and a machine that "
             "is not consuming is capital sitting in somebody else's "
             "building. This is also what makes a slow-moving reagent "
             "readable: dead stock, or the only consumable for a machine "
             "placed last month.",
    ),
    Module(
        id="service",
        name="Service",
        title="What you serviced",
        blurb="Jobs, the hours and parts that went in against what "
              "was billed, and the repeat visits nobody counts.",
        engine="service.evaluate",
        needs=("jobs",),
        section="service",
        process=(
            "Log each job against the machine it was for.",
            "Track what it cost and what it was billed at.",
            "Watch which machines keep coming back.",
        ),
        why=
             "Service is where a placement quietly stops paying. A machine "
             "under warranty that needs three visits a quarter is costing "
             "the margin on its own consumables, and nothing else in the "
             "business is counting that.",
    ),
    Module(
        id="quality",
        name="Checks",
        title="Is this right",
        blurb="What is wrong with your figures before you act on them: "
              "duplicates, impossible dates, costs out by a hundred.",
        engine="sanity.check",
        needs=("items",),
        uses=("demand", "invoices", "jobs", "placements", "commitments"),
        # Not staff. The checks read across invoices, placements and
        # commitments, and a role that cannot see those would be shown
        # a report whose every "nothing recorded" was a permission
        # rather than a fact.
        roles=("owner", "manager", "accountant"),
        group_level=True,
        section="decide",
        process=(
            "Read what the engine says it cannot stand behind.",
            "Work down: a figure that does not reconcile, a record that contradicts another, a gap that makes an answer unsafe.",
            "Fix the record, not the figure.",
            "Come back after an import, which is when most of these appear.",
        ),
        why=
             "Every other screen asserts something, and most of it rests "
             "on a handful of figures an importer enters by hand: an FX "
             "rate on a receipt, a duty line against the right shipment, "
             "a lead time somebody guessed in 2019. This is the screen "
             "that says which assertions are thin. Software that quietly "
             "computes over a bad landed cost is worse than software "
             "that stops, because the first kind is believed.",
        whole_business=
                 "A duplicate or a contradiction is usually between two "
                 "branches, so narrowing to one would hide exactly the "
                 "faults worth finding.",
    ),
    Module(
        id="intelligence",
        name="Pipeline",
        title="What to do next",
        blurb="Tenders and accounts being worked, weighted by odds "
              "actually observed, checked against stock and cash.",
        engine="marketing.evaluate",
        needs=("opportunities",),
        uses=("items", "invoices", "commitments"),
        roles=("owner", "manager"),
        group_level=True,
        section="decide",
        process=(
            "Read the list in the order given — it is ranked by what it is worth, not by module.",
            "Take each one to the screen it came from.",
            "Ignore the ones you have already decided; the ranking will move on.",
        ),
        why=
             "Fourteen screens is thirteen more than most mornings have room "
             "for. This is the one that reads them all and says which three "
             "things are worth the attention today, with the money attached "
             "so the order is arguable rather than asserted.",
        whole_business=
                 "The ranking reads every module at once, and the transfer "
                 "that avoids an import is only visible from above both "
                 "branches.",
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
            "name": m.name,
            "title": m.title,
            "blurb": m.blurb,
            # What somebody does here and why it is worth doing. Sent
            # with the rail rather than fetched per screen: it is a
            # few hundred bytes a module and it means a screen that
            # cannot load its figures can still say what it is for.
            "process": list(m.process),
            "why": m.why,
            "whole_business": m.whole_business,
            "engine": m.engine,
            "section": m.section,
            "group_level": m.group_level,
            "state": state,
            "missing": list(m.missing(present)),
            "needs": list(m.needs),
            "uses": list(m.uses),
        })
    return out
