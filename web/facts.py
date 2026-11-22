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

from engine import report
from engine.db import snapshot
from engine.schema import Thresholds


_UNSET = object()


class Facts:
    """Lazily-read records and derived results for one request.

    `missing` collects the record types a read found empty, and
    `refused` the ones the policies would not show this user. They are
    different answers to "why is this module blank" and a screen that
    conflated them would tell an accountant to go and enter figures
    that are already there.
    """

    def __init__(self, conn, thresholds: Thresholds | None = None,
                 as_of: date | None = None, currency: str = "PKR") -> None:
        self.conn = conn
        self.thresholds = thresholds or Thresholds()
        self.as_of = as_of or date.today()
        self.currency = currency
        self._cache: dict[str, Any] = {}
        self.refused: set[str] = set()

    # ---------------------------------------------------------- plumbing

    def _get(self, name: str, read: Callable[[], Any], empty: Any) -> Any:
        """Read once, remember, and turn a refusal into an empty answer.

        A policy refusal is recorded rather than raised because the
        caller is rendering a page, not performing a transaction: the
        right outcome is a module that says it is not available to this
        role, not a 500 on a screen that asked for something else.
        """
        if name in self._cache:
            return self._cache[name]
        try:
            value = read()
        except Exception:
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
    def items(self):
        s = self.snapshot
        return list(s.items) if s else []

    @property
    def demand(self):
        s = self.snapshot
        return list(s.demand) if s else []

    @property
    def invoices(self):
        return self._get("invoices",
                         lambda: snapshot.read_invoices(self.conn), [])

    @property
    def jobs(self):
        return self._get("jobs",
                         lambda: snapshot.read_jobs(self.conn), [])

    @property
    def placements(self):
        return self._get("placements",
                         lambda: snapshot.read_machines(self.conn), [])

    @property
    def commitments(self):
        return self._get(
            "commitments",
            lambda: snapshot.read_commitments(self.conn, self.as_of), [])

    @property
    def accounts(self):
        return self._get(
            "accounts",
            lambda: snapshot.read_accounts(self.conn, self.as_of), [])

    @property
    def employees(self):
        return self._get("employees",
                         lambda: snapshot.read_employees(self.conn), [])

    @property
    def opportunities(self):
        return self._get("opportunities",
                         lambda: snapshot.read_opportunities(self.conn), [])

    @property
    def marketing_spend(self):
        return self._get(
            "marketing_spend",
            lambda: snapshot.read_marketing_spend(self.conn), [])

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
    def transfers(self):
        if "transfers" not in self._cache:
            from engine.modules.redeploy import suggest
            self._cache["transfers"] = report._transfer_rows(
                suggest(self.blocks, self.thresholds))
        return self._cache["transfers"]

    @property
    def cash(self):
        """Module 8. Deliberately excludes new orders; see sequencing."""
        if "cash" not in self._cache:
            from engine.modules.cash import evaluate
            have = (self.invoices or self.commitments
                    or self.placements or self.jobs)
            self._cache["cash"] = evaluate(
                invoices=self.invoices,
                commitments=self.commitments,
                placements=self.placements,
                jobs=self.jobs,
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
