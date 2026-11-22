"""Module 10. Profit and loss, balance sheet, and cash flow from both.

The cash flow is derived rather than entered. That is the whole point: a
cash flow statement typed in by hand agrees with nothing, while one built
from the movement between two balance sheets plus the period's profit is
forced to agree with both. Where it does not, something upstream is wrong,
and this module says so instead of presenting a tidy statement.

Three checks run every time, and all three are reported:

  The balance sheet balances. Assets less liabilities less equity is zero at
  the opening date and at the closing date.

  Retained earnings move by the profit. Closing retained earnings less
  opening, plus any dividend, equals profit after tax. This is what catches
  an entry posted to the wrong side.

  The cash flow ties. The net movement the statement computes equals the
  movement in cash on the balance sheet. An indirect cash flow that does not
  tie is not a presentational problem; it means a working-capital movement
  has been missed.

Accounts are mapped to statement lines explicitly, never guessed from their
names. "Insurance" could be an expense or a prepayment, and a product that
guesses puts the difference in profit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

from engine.schema import Thresholds

# What each account is, for the statements. The left of each pair is the
# statement it belongs to.
PNL_LINES = (
    "revenue", "cogs", "payroll", "opex", "depreciation",
    "finance_cost", "other_income", "tax",
)
BS_ASSETS = (
    "cash", "receivables", "inventory", "other_current_assets",
    "fixed_assets",
)
BS_CONTRA_ASSETS = ("accumulated_depreciation",)
BS_LIABILITIES = (
    "payables", "short_term_debt", "long_term_debt", "other_liabilities",
    "tax_payable",
)
BS_EQUITY = ("equity", "retained_earnings")

ALL_LINES = (PNL_LINES + BS_ASSETS + BS_CONTRA_ASSETS
             + BS_LIABILITIES + BS_EQUITY)

# Working capital lines, and which way an increase moves cash.
WORKING_CAPITAL = {
    "receivables": -1,            # more owed to us is cash we have not got
    "inventory": -1,
    "other_current_assets": -1,
    "payables": +1,               # more owed by us is cash we still hold
    "other_liabilities": +1,
    "tax_payable": +1,
}


@dataclass(frozen=True)
class Account:
    """One line of the trial balance, in natural positive amounts.

    Debits and credits are the accountant's convention; a distributor filling
    in a sheet is far likelier to get "revenue 4,000,000" right than
    "revenue credit 4,000,000". The line tells the engine the sign.
    """

    entity_id: str
    code: str
    name: str
    line: str
    opening: float = 0.0
    closing: float = 0.0

    @property
    def movement(self) -> float:
        return self.closing - self.opening


@dataclass(frozen=True)
class Check:
    name: str
    expected: float
    actual: float
    difference: float
    passed: bool
    meaning: str


@dataclass(frozen=True)
class Statements:
    period_from: date
    period_to: date

    revenue: float
    cogs: float
    gross_profit: float
    gross_margin: float
    payroll: float
    opex: float
    depreciation: float
    operating_profit: float
    finance_cost: float
    other_income: float
    profit_before_tax: float
    tax: float
    profit_after_tax: float
    net_margin: float

    assets_opening: float
    assets_closing: float
    liabilities_closing: float
    equity_closing: float
    cash_opening: float
    cash_closing: float
    working_capital: float
    current_ratio: float | None
    gearing: float | None

    cash_from_operations: float
    cash_from_investing: float
    cash_from_financing: float
    net_cash_movement: float
    operating_lines: tuple
    investing_lines: tuple
    financing_lines: tuple

    pnl_rows: tuple
    bs_rows: tuple
    checks: tuple
    ties: bool
    reason: str
    caveat: str


def _sum(accounts, line, which="closing"):
    return round(sum(getattr(a, which) for a in accounts if a.line == line), 2)


def _movement(accounts, line):
    return round(sum(a.movement for a in accounts if a.line == line), 2)


def unknown_lines(accounts: Iterable[Account]) -> list[str]:
    return sorted({a.line for a in accounts if a.line not in ALL_LINES})


def evaluate(
    accounts: Sequence[Account],
    thresholds: Thresholds | None = None,
    period_from: date | None = None,
    period_to: date | None = None,
    dividends: float = 0.0,
) -> Statements:
    thresholds = thresholds or Thresholds()
    period_to = period_to or date.today()
    period_from = period_from or date(period_to.year, 1, 1)
    accounts = [a for a in accounts if a.line in ALL_LINES]

    # ------------------------------------------------------------- profit
    revenue = _sum(accounts, "revenue")
    cogs = _sum(accounts, "cogs")
    gross = round(revenue - cogs, 2)
    payroll = _sum(accounts, "payroll")
    opex = _sum(accounts, "opex")
    depreciation = _sum(accounts, "depreciation")
    operating = round(gross - payroll - opex - depreciation, 2)
    finance_cost = _sum(accounts, "finance_cost")
    other_income = _sum(accounts, "other_income")
    pbt = round(operating - finance_cost + other_income, 2)
    tax = _sum(accounts, "tax")
    pat = round(pbt - tax, 2)

    # ------------------------------------------------------ balance sheet
    def assets(which):
        gross_assets = sum(_sum(accounts, l, which) for l in BS_ASSETS)
        contra = sum(_sum(accounts, l, which) for l in BS_CONTRA_ASSETS)
        return round(gross_assets - contra, 2)

    def liabilities(which):
        return round(sum(_sum(accounts, l, which) for l in BS_LIABILITIES), 2)

    def equity(which):
        return round(sum(_sum(accounts, l, which) for l in BS_EQUITY), 2)

    cash_open = _sum(accounts, "cash", "opening")
    cash_close = _sum(accounts, "cash", "closing")

    current_assets = round(sum(
        _sum(accounts, l) for l in
        ("cash", "receivables", "inventory", "other_current_assets")), 2)
    current_liabilities = round(sum(
        _sum(accounts, l) for l in
        ("payables", "short_term_debt", "other_liabilities", "tax_payable")), 2)
    debt = round(_sum(accounts, "short_term_debt")
                 + _sum(accounts, "long_term_debt"), 2)

    # --------------------------------------------------------- cash flow
    # Indirect: start at profit, add back what never moved cash, then undo
    # the effect of everything that moved on the balance sheet.
    operating_lines = [("Profit after tax", pat),
                       ("Depreciation", depreciation)]
    for line, direction in WORKING_CAPITAL.items():
        move = _movement(accounts, line)
        if move:
            operating_lines.append((_label(line, move), round(direction * move, 2)))
    cfo = round(sum(v for _n, v in operating_lines), 2)

    # Investing: what was spent on fixed assets, at cost, so depreciation is
    # not double counted.
    capex = _movement(accounts, "fixed_assets")
    investing_lines = []
    if capex:
        investing_lines.append(
            ("Bought fixed assets" if capex > 0 else "Sold fixed assets",
             round(-capex, 2)))
    cfi = round(sum(v for _n, v in investing_lines), 2)

    financing_lines = []
    for line in ("short_term_debt", "long_term_debt", "equity"):
        move = _movement(accounts, line)
        if move:
            financing_lines.append((_label(line, move), round(move, 2)))
    if dividends:
        financing_lines.append(("Dividends paid", round(-abs(dividends), 2)))
    cff = round(sum(v for _n, v in financing_lines), 2)

    net_movement = round(cfo + cfi + cff, 2)

    # ------------------------------------------------------------ checks
    tol = 1.0
    checks = (
        Check("The balance sheet balances at the start",
              round(liabilities("opening") + equity("opening"), 2),
              assets("opening"),
              round(assets("opening") - liabilities("opening")
                    - equity("opening"), 2),
              abs(assets("opening") - liabilities("opening")
                  - equity("opening")) <= tol,
              "Assets must equal what is owed plus what is owned."),
        Check("The balance sheet balances at the end",
              round(liabilities("closing") + equity("closing"), 2),
              assets("closing"),
              round(assets("closing") - liabilities("closing")
                    - equity("closing"), 2),
              abs(assets("closing") - liabilities("closing")
                  - equity("closing")) <= tol,
              "Same again at the period end. If the start balanced and the "
              "end does not, something was posted during the period that "
              "only went to one side of the books."),
        Check("Retained earnings moved by the profit",
              pat,
              round(_movement(accounts, "retained_earnings") + dividends, 2),
              round(_movement(accounts, "retained_earnings") + dividends - pat, 2),
              abs(_movement(accounts, "retained_earnings") + dividends - pat)
              <= tol,
              "What the business earned should be what its reserves gained, "
              "once dividends are added back. This is what catches an entry "
              "posted to the wrong side."),
        Check("The cash flow ties to the cash balance",
              round(cash_close - cash_open, 2),
              net_movement,
              round(net_movement - (cash_close - cash_open), 2),
              abs(net_movement - (cash_close - cash_open)) <= tol,
              "The statement's net movement must equal the change in cash on "
              "the balance sheet. If it does not, a working-capital movement "
              "has been missed and the statement is wrong, not untidy."),
    )
    ties = all(c.passed for c in checks)

    reason = _reason(revenue, gross, pat, cfo, net_movement,
                     cash_open, cash_close, ties)

    return Statements(
        period_from=period_from, period_to=period_to,
        revenue=revenue, cogs=cogs, gross_profit=gross,
        gross_margin=round(gross / revenue, 4) if revenue else 0.0,
        payroll=payroll, opex=opex, depreciation=depreciation,
        operating_profit=operating, finance_cost=finance_cost,
        other_income=other_income, profit_before_tax=pbt, tax=tax,
        profit_after_tax=pat,
        net_margin=round(pat / revenue, 4) if revenue else 0.0,
        assets_opening=assets("opening"), assets_closing=assets("closing"),
        liabilities_closing=liabilities("closing"),
        equity_closing=equity("closing"),
        cash_opening=cash_open, cash_closing=cash_close,
        working_capital=round(current_assets - current_liabilities, 2),
        current_ratio=round(current_assets / current_liabilities, 2)
            if current_liabilities else None,
        gearing=round(debt / (debt + equity("closing")), 4)
            if (debt + equity("closing")) else None,
        cash_from_operations=cfo, cash_from_investing=cfi,
        cash_from_financing=cff, net_cash_movement=net_movement,
        operating_lines=tuple(operating_lines),
        investing_lines=tuple(investing_lines),
        financing_lines=tuple(financing_lines),
        pnl_rows=_pnl_rows(accounts), bs_rows=_bs_rows(accounts),
        checks=checks, ties=ties,
        reason=reason,
        caveat=(
            "Built from the trial balance as entered. Accounts are mapped to "
            "statement lines by the mapping on the sheet, never guessed from "
            "their names, so a misrouted account moves a figure here without "
            "breaking anything."
        ),
    )


def _label(line, move):
    names = {
        "receivables": ("Receivables up", "Receivables down"),
        "inventory": ("Stock up", "Stock down"),
        "other_current_assets": ("Other current assets up",
                                 "Other current assets down"),
        "payables": ("Payables up", "Payables down"),
        "other_liabilities": ("Other liabilities up", "Other liabilities down"),
        "tax_payable": ("Tax payable up", "Tax payable down"),
        "short_term_debt": ("Short-term borrowing", "Short-term repayment"),
        "long_term_debt": ("Long-term borrowing", "Long-term repayment"),
        "equity": ("Capital introduced", "Capital withdrawn"),
    }
    up, down = names.get(line, (line, line))
    return up if move > 0 else down


def _money(v):
    """One formatter, so a figure reads the same everywhere.

    This was a private copy of engine/fmt.money with the scale
    hard-coded to millions -- one of four such copies, which is
    how the same amount came to appear in two notations on one
    screen.
    """
    from engine.fmt import money
    return money(v)


def _reason(revenue, gross, pat, cfo, net, cash_open, cash_close, ties):
    if not revenue and not cash_close:
        return "No trial balance was supplied, so there are no statements."
    bits = [
        f"{_money(revenue)} of revenue produced {_money(gross)} of gross "
        f"profit and {_money(pat)} after tax."
    ]
    if cfo < pat:
        bits.append(
            f"Operations turned that into {_money(cfo)} of cash: the "
            f"difference is working capital, which is profit you have earned "
            f"and not yet collected."
        )
    else:
        bits.append(f"Operations produced {_money(cfo)} of cash.")
    bits.append(
        f"Cash went from {_money(cash_open)} to {_money(cash_close)}.")
    if not ties:
        bits.append(
            "These statements do not tie to each other. Read the checks "
            "before using any figure here.")
    return " ".join(bits)


def _pnl_rows(accounts):
    rows = []
    for line in PNL_LINES:
        for a in accounts:
            if a.line == line and (a.closing or a.opening):
                rows.append({
                    "entity_id": a.entity_id, "code": a.code, "name": a.name,
                    "line": line, "amount": round(a.closing, 2),
                })
    return tuple(rows)


def _bs_rows(accounts):
    rows = []
    for line in BS_ASSETS + BS_CONTRA_ASSETS + BS_LIABILITIES + BS_EQUITY:
        for a in accounts:
            if a.line == line and (a.closing or a.opening):
                side = ("asset" if line in BS_ASSETS + BS_CONTRA_ASSETS
                        else "liability" if line in BS_LIABILITIES else "equity")
                rows.append({
                    "entity_id": a.entity_id, "code": a.code, "name": a.name,
                    "line": line, "side": side,
                    "opening": round(a.opening, 2),
                    "closing": round(a.closing, 2),
                    "movement": round(a.movement, 2),
                })
    return tuple(rows)
