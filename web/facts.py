"""One organisation's records, read once and only when asked for.

Every module needs a different subset of the same dozen record types,
and several need the same ones -- the cash view and the order
sequencing both start from invoices, and three modules want the
classified stock blocks. So this is a lazy cache: a property per record
type, read on first use and held for the life of the request.

Two reasons it is not simply `report.build()`, which already
orchestrates every module.

The first is cost. `build` reads and computes everything, so opening
the stock screen would run payroll, the statements and the pipeline.
That is thirteen tables and eleven modules to render one table.

The second is access. The policies refuse payroll to a branch manager
and the statements to anyone but an owner or an accountant. `build`
reading them unconditionally turns "this role may not see the books"
into a failed request on an unrelated screen. Here a module reads what
it needs, so a refusal lands on the module that asked.

Dependency order is still enforced, in the one place it matters:
`cash` must run before `sequencing`, because the cash view deliberately
excludes new orders so its headroom is what the sequencing packs into.
Computing them together would be circular. The properties below make
that order structural rather than something a caller remembers.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from engine import report, scope as scoping
from engine.db import snapshot
from engine.schema import Thresholds


_UNSET = object()


# What counts as "the policies will not show you this", as opposed to
# "this query is broken". The first is a fact about the person and
# belongs on the screen; the second is a fact about the code and
# belongs in the logs with a 500 next to it.
#
# A filtered SELECT under row-level security returns no rows rather
# than raising, so these are the narrower cases: a grant the role does
# not hold, and a table or column a migration has not created here
# yet. Everything else propagates.
try:  # pragma: no cover - exercised wherever psycopg is installed
    from psycopg import errors as _pg

    _REFUSALS: tuple = (
        _pg.InsufficientPrivilege,
        _pg.UndefinedTable,
        _pg.UndefinedColumn,
        _pg.UndefinedFunction,
    )
except Exception:  # pragma: no cover - psycopg absent in some tooling
    _REFUSALS = ()


class Facts:
    """Lazily-read records and derived results for one request.

    `missing` collects the record types a read found empty, and
    `refused` the ones the policies would not show this user. They are
    different answers to "why is this module blank" and a screen that
    conflated them would tell an accountant to go and enter figures
    that are already there.
    """

    def __init__(self, conn, thresholds: Thresholds | None = None,
                 as_of: date | None = None, currency: str = "PKR",
                 branch: str | None = None) -> None:
        self.conn = conn
        self.thresholds = thresholds or Thresholds()
        self.as_of = as_of or date.today()
        self.currency = currency
        # Narrowing happens here, before any module runs, which is the
        # only place it can be right. engine/scope.py sets out why at
        # length: a row filter in the page narrows the list and not the
        # figures above it, and several figures cannot be recovered
        # from filtered rows at all -- safety stock comes from a
        # location's own history, collection days are a ratio against
        # revenue, and forecast error is measured per line.
        self.scope = scoping.read(None, branch or None)
        self._cache: dict[str, Any] = {}
        self.refused: set[str] = set()

    # ---------------------------------------------------------- plumbing

    def _narrow(self, name: str, rows):
        """The rows this scope can see, and nothing else.

        Two record types narrow by company only: a trial balance
        belongs to a company, and there is no Karachi balance sheet
        unless somebody keeps one. Filtering its rows would invent one.
        """
        if not self.scope.active:
            return rows
        if name in scoping.ENTITY_ONLY:
            return scoping.filter_records(
                rows, scoping.Scope(entity=self.scope.entity))
        return scoping.filter_records(rows, self.scope)

    def _get(self, name: str, read: Callable[[], Any], empty: Any) -> Any:
        """Read once, remember, and turn a refusal into an empty answer.

        A policy refusal is recorded rather than raised because the
        caller is rendering a page, not performing a transaction: the
        right outcome is a module that says it is not available to this
        role, not a 500 on a screen that asked for something else.

        Only a refusal, though. This caught every exception and filed
        it under `refused`, so a column renamed in a migration, a
        typo in a query or a dropped connection all reached the person
        as "your role cannot see this" -- the one explanation that
        sends them to an administrator instead of to the logs.

        Worse, one failure poisoned the rest. A Postgres transaction
        that has raised refuses every later statement with
        InFailedSqlTransaction, so a single broken query turned every
        remaining module on the page into a permission problem.
        That one is re-raised deliberately: it means an earlier error
        was swallowed, and the honest answer is the error, not
        fourteen empty screens.
        """
        if name in self._cache:
            return self._cache[name]
        try:
            value = read()
        except _REFUSALS:
            self.refused.add(name)
            value = empty
        self._cache[name] = value
        return value

    # ------------------------------------------------------------ records

    @property
    def snapshot(self):
        return self._get(
            "snapshot",
            lambda: snapshot.read(self.conn, self.as_of, self.currency),
            None,
        )

    @property
    def group_items(self):
        s = self.snapshot
        return list(s.items) if s else []

    @property
    def group_demand(self):
        s = self.snapshot
        return list(s.demand) if s else []

    @property
    def items(self):
        return self._narrow("items", self.group_items)

    @property
    def demand(self):
        return self._narrow("demand", self.group_demand)

    @property
    def group_invoices(self):
        return self._get("invoices",
                         lambda: snapshot.read_invoices(self.conn), [])

    @property
    def invoices(self):
        return self._narrow("invoices", self.group_invoices)

    @property
    def group_jobs(self):
        return self._get("jobs",
                         lambda: snapshot.read_jobs(self.conn), [])

    @property
    def jobs(self):
        return self._narrow("jobs", self.group_jobs)

    @property
    def group_placements(self):
        return self._get("placements",
                         lambda: snapshot.read_machines(self.conn), [])

    @property
    def placements(self):
        return self._narrow("placements", self.group_placements)

    @property
    def group_commitments(self):
        return self._get(
            "commitments",
            lambda: snapshot.read_commitments(self.conn, self.as_of), [])

    @property
    def commitments(self):
        return self._narrow("commitments", self.group_commitments)

    @property
    def group_accounts(self):
        return self._get(
            "accounts",
            lambda: snapshot.read_accounts(self.conn, self.as_of), [])

    @property
    def accounts(self):
        return self._narrow("accounts", self.group_accounts)

    @property
    def group_employees(self):
        return self._get("employees",
                         lambda: snapshot.read_employees(self.conn), [])

    @property
    def employees(self):
        return self._narrow("employees", self.group_employees)

    @property
    def group_opportunities(self):
        return self._get("opportunities",
                         lambda: snapshot.read_opportunities(self.conn), [])

    @property
    def opportunities(self):
        return self._narrow("opportunities", self.group_opportunities)

    @property
    def group_marketing_spend(self):
        return self._get(
            "marketing_spend",
            lambda: snapshot.read_marketing_spend(self.conn), [])

    @property
    def marketing_spend(self):
        return self._narrow("marketing_spend", self.group_marketing_spend)

    @property
    def opening_balance(self):
        """The bank balance the cash view opens from, or None.

        None rather than zero when nobody has entered one. Zero is a
        balance, and a cash view opening at zero describes a business
        about to miss payroll.
        """
        got = self._get(
            "opening_balance",
            lambda: snapshot.read_cash_balance(self.conn, self.as_of),
            (None, None))
        return got[0]

    @property
    def balance_as_at(self):
        got = self._get(
            "opening_balance",
            lambda: snapshot.read_cash_balance(self.conn, self.as_of),
            (None, None))
        return got[1]

    @property
    def monthly_revenue(self):
        """Revenue a month, and where the figure came from.

        DSO needs it, and engine/modules/receivables.py will not
        estimate it from the receivables -- outstanding divided by a
        rate derived from outstanding is circular and would produce a
        DSO that moves when nothing has changed.

        Two honest sources, in order. The revenue line in the trial
        balance is the audited one, so it wins where the books are
        loaded. Otherwise it is invoices raised over the months they
        span, which is sales in the period and not the open balance,
        so it is the ordinary basis for DSO rather than a workaround.

        Returns (amount, basis). The basis reaches the screen because
        a collection figure computed off invoices and one computed off
        audited revenue are not the same claim, and the person reading
        it should be able to tell which they have.
        """
        if "monthly_revenue" in self._cache:
            return self._cache["monthly_revenue"]

        got = (None, None)
        for account in self.accounts:
            if account.line == "revenue" and account.closing:
                from engine.db import snapshot
                period_from, period_to = snapshot.accounts_period(
                    self.conn, self.as_of)
                months = 12.0
                if period_from and period_to:
                    months = max(
                        1.0, (period_to - period_from).days / 30.44)
                got = (round(account.closing / months, 2), "books")
                break
        else:
            billed = [i.amount for i in self.invoices]
            if billed:
                dates = [i.invoice_date for i in self.invoices]
                span = max(
                    1.0, (max(dates) - min(dates)).days / 30.44)
                got = (round(sum(billed) / span, 2), "invoices")

        self._cache["monthly_revenue"] = got
        return got

    @property
    def present(self) -> set[str]:
        return self._get("present", lambda: snapshot.present(self.conn), set())

    # ----------------------------------------------------------- derived

    @property
    def blocks(self):
        """Stock grouped per branch, each with its own classification.

        Per branch rather than per company because demand, and
        therefore policy, is local: the same line can be smooth in
        Karachi and intermittent in Lahore, and classifying the pooled
        series gets both wrong.
        """
        if "blocks" not in self._cache:
            self._cache["blocks"] = report.build_blocks(
                self.items, self.demand, self.thresholds, self.as_of)
        return self._cache["blocks"]

    @property
    def stock(self):
        if "stock" not in self._cache:
            self._cache["stock"] = report._stock_rows(self.blocks)
        return self._cache["stock"]

    @property
    def group_blocks(self):
        """Every branch's blocks, whatever the scope.

        A transfer has a sender and a receiver. Narrow the facts to one
        branch and the module can no longer see the other side of the
        move it exists to suggest, so it would return nothing and look
        like there was nothing to move.
        """
        if not self.scope.active:
            return self.blocks
        if "group_blocks" not in self._cache:
            self._cache["group_blocks"] = report.build_blocks(
                self.group_items, self.group_demand,
                self.thresholds, self.as_of)
        return self._cache["group_blocks"]

    @property
    def transfers(self):
        """Computed across the group, then narrowed to the moves this
        branch is party to -- in or out, because a branch cares about
        stock arriving as much as about stock leaving."""
        if "transfers" not in self._cache:
            from engine.modules.redeploy import suggest
            rows = report._transfer_rows(
                suggest(self.group_blocks, self.thresholds))
            if self.scope.active and self.scope.location:
                here = self.scope.location
                rows = [t for t in rows
                        if here in (t.get("from_location"),
                                    t.get("to_location"))]
            self._cache["transfers"] = rows
        return self._cache["transfers"]

    @property
    def cash(self):
        """Module 8. Deliberately excludes new orders; see sequencing."""
        if "cash" not in self._cache:
            from engine.modules.cash import evaluate
            # On the group, always. One bank balance serves the whole
            # business: running thirteen weeks for one branch against
            # the group's opening balance would invent a balance that
            # branch does not have, and splitting the balance between
            # branches is a treasury decision nobody has made.
            have = (self.group_invoices or self.group_commitments
                    or self.group_placements or self.group_jobs)
            self._cache["cash"] = evaluate(
                invoices=self.group_invoices,
                commitments=self.group_commitments,
                placements=self.group_placements,
                jobs=self.group_jobs,
                thresholds=self.thresholds,
                as_of=self.as_of,
                opening_balance=self.opening_balance,
            ) if have else None
        return self._cache["cash"]

    @property
    def sequencing(self):
        """Module 9, which packs orders into module 8's headroom.

        Reading `self.cash` here is what makes the order
        non-negotiable: the sequencing cannot be computed without the
        cash view, so it cannot accidentally be computed first.
        """
        if "sequencing" not in self._cache:
            from engine.modules.sequence import evaluate
            self._cache["sequencing"] = evaluate(
                self.stock, self.cash, self.transfers,
                self.thresholds, self.as_of) if self.stock else None
        return self._cache["sequencing"]

    # ------------------------------------------------- shared results
    #
    # Five module results that were each computed inside the runner
    # that printed them. The ranked cross-module list needs all five
    # at once, and computing them a second time to rank them would
    # mean the home screen and the ranking could disagree about the
    # same business on the same request. They are cached here like
    # everything else, so a request that reads one module pays for one
    # and a request that reads all of them pays once each.

    @property
    def receivables(self):
        """What is owed, aged. None when there are no invoices."""
        if "receivables" not in self._cache:
            from engine.modules.receivables import evaluate
            revenue, _basis = self.monthly_revenue
            self._cache["receivables"] = evaluate(
                self.invoices, self.thresholds, self.as_of,
                monthly_revenue=revenue) if self.invoices else None
        return self._cache["receivables"]

    @property
    def service(self):
        """Service jobs and what they cost. None when there are none."""
        if "service" not in self._cache:
            from engine.modules.service import evaluate
            self._cache["service"] = evaluate(
                self.jobs, self.thresholds, self.as_of) \
                if self.jobs else None
        return self._cache["service"]

    @property
    def machines(self):
        """Placements, as rows. Empty when nothing is placed."""
        if "machines" not in self._cache:
            from engine import report
            from engine.modules.machines import evaluate
            results = evaluate(self.placements, self.thresholds, self.as_of) \
                if self.placements else ()
            self._cache["machines"] = report._machine_rows(results)
        return self._cache["machines"]

    @property
    def forecasts(self):
        """A forecast per line per branch, as rows."""
        if "forecasts" not in self._cache:
            from engine import report
            from engine.modules.forecast import evaluate
            results = []
            for block in self.blocks:
                results.extend(evaluate(
                    block.items,
                    [d for d in self.demand
                     if d.location_id == block.location_id],
                    block.classes, self.thresholds, self.as_of,
                ))
            self._cache["forecasts"] = report._forecast_rows(results)
        return self._cache["forecasts"]

    @property
    def findings(self):
        """What the sanity checks say is wrong with the figures."""
        if "findings" not in self._cache:
            from engine import sanity
            self._cache["findings"] = sanity.check(
                items=self.items, demand=self.demand,
                invoices=self.invoices, jobs=self.jobs,
                placements=self.placements,
                commitments=self.commitments, as_of=self.as_of,
            )
        return self._cache["findings"]
