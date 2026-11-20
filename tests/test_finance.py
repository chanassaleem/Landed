"""Profit and loss, balance sheet, and the cash flow derived from both.

The cash flow is the reason these exist. One typed in by hand agrees with
nothing; one built from the movement between two balance sheets plus the
period's profit is forced to agree with both, and when it does not that is
a fault worth reporting rather than hiding.
"""

from datetime import date

import pytest

from engine.modules.finance import Account, evaluate, unknown_lines


def tb(**over):
    """A year that articulates. Cash closes at 3,840,000 because that is
    what the cash flow produces: 4,000,000 opening less 160,000."""
    rows = {
        "revenue": 48_000_000, "cogs": 31_200_000, "payroll": 7_800_000,
        "opex": 3_600_000, "depreciation": 1_200_000, "finance_cost": 900_000,
        "tax": 960_000,
    }
    rows.update({k: v for k, v in over.items() if k in rows})
    accounts = [Account("DX", c, c.title(), c, 0, v) for c, v in rows.items()]

    bs = {
        "cash": (4_000_000, 3_840_000),
        "receivables": (12_000_000, 14_000_000),
        "inventory": (9_000_000, 10_500_000),
        "fixed_assets": (14_000_000, 16_000_000),
        "accumulated_depreciation": (5_000_000, 6_200_000),
        "payables": (8_000_000, 9_300_000),
        "short_term_debt": (6_000_000, 6_500_000),
        "equity": (5_000_000, 5_000_000),
        "retained_earnings": (15_000_000, 17_340_000),
    }
    bs.update({k: v for k, v in over.items() if k in bs})
    accounts += [Account("DX", k, k.title(), k, o, c) for k, (o, c) in bs.items()]
    return accounts


def run(accounts=None, **kw):
    return evaluate(accounts if accounts is not None else tb(),
                    period_from=date(2026, 1, 1),
                    period_to=date(2026, 12, 31), **kw)


# -------------------------------------------------------- profit and loss

def test_the_profit_and_loss_adds_up():
    s = run()
    assert s.gross_profit == 48_000_000 - 31_200_000
    assert s.operating_profit == pytest.approx(
        16_800_000 - 7_800_000 - 3_600_000 - 1_200_000)
    assert s.profit_before_tax == pytest.approx(4_200_000 - 900_000)
    assert s.profit_after_tax == pytest.approx(3_300_000 - 960_000)
    assert s.gross_margin == pytest.approx(0.35, abs=0.001)


def test_margins_do_not_divide_by_zero_without_revenue():
    s = run([Account("DX", "1000", "Bank", "cash", 0, 0)])
    assert s.gross_margin == 0 and s.net_margin == 0


# ---------------------------------------------------------- balance sheet

def test_the_balance_sheet_balances_and_says_so():
    s = run()
    opening, closing = s.checks[0], s.checks[1]
    assert opening.passed and closing.passed
    assert s.assets_closing == pytest.approx(
        s.liabilities_closing + s.equity_closing, abs=1)


def test_accumulated_depreciation_is_taken_off_the_assets():
    s = run()
    # 3,840,000 + 14,000,000 + 10,500,000 + 16,000,000 - 6,200,000
    assert s.assets_closing == pytest.approx(38_140_000, abs=1)


def test_working_capital_and_the_ratios():
    s = run()
    # current assets 28,340,000 less current liabilities 15,800,000
    assert s.working_capital == pytest.approx(12_540_000, abs=1)
    assert s.current_ratio == pytest.approx(1.79, abs=0.01)
    assert s.gearing == pytest.approx(6_500_000 / (6_500_000 + 22_340_000),
                                      abs=0.001)


# -------------------------------------------------------------- cash flow

def test_the_cash_flow_is_derived_and_ties_to_the_cash_balance():
    s = run()
    assert s.ties
    assert s.net_cash_movement == pytest.approx(
        s.cash_closing - s.cash_opening, abs=1)


def test_the_operating_section_is_profit_plus_non_cash_less_working_capital():
    s = run()
    # 2,340,000 + 1,200,000 - 2,000,000 - 1,500,000 + 1,300,000
    assert s.cash_from_operations == pytest.approx(1_340_000, abs=1)
    names = [n for n, _v in s.operating_lines]
    assert "Profit after tax" in names
    assert "Depreciation" in names
    assert "Receivables up" in names and "Stock up" in names


def test_capital_spending_is_investing_not_operating():
    s = run()
    assert s.cash_from_investing == pytest.approx(-2_000_000, abs=1)
    assert ("Bought fixed assets", -2_000_000) in s.investing_lines


def test_borrowing_is_financing():
    s = run()
    assert s.cash_from_financing == pytest.approx(500_000, abs=1)


def test_a_dividend_is_financing_and_explains_retained_earnings():
    """Paying one breaks the retained-earnings check unless it is told."""
    broken = run(tb(retained_earnings=(15_000_000, 16_340_000)))
    assert not broken.checks[2].passed
    told = evaluate(tb(retained_earnings=(15_000_000, 16_340_000),
                       cash=(4_000_000, 2_840_000)),
                    period_from=date(2026, 1, 1), period_to=date(2026, 12, 31),
                    dividends=1_000_000)
    assert told.checks[2].passed
    assert ("Dividends paid", -1_000_000) in told.financing_lines


# ----------------------------------------------------------- the checking

def test_an_entry_on_the_wrong_side_is_caught_by_retained_earnings():
    """The classic error: profit posted somewhere that is not reserves."""
    s = run(tb(retained_earnings=(15_000_000, 15_000_000)))
    check = s.checks[2]
    assert not check.passed
    assert check.difference == pytest.approx(-2_340_000, abs=1)
    assert not s.ties


def test_a_missed_working_capital_movement_breaks_the_tie():
    s = run(tb(receivables=(12_000_000, 20_000_000)))
    assert not s.ties
    assert not s.checks[3].passed


def test_a_statement_that_does_not_tie_says_so_in_the_sentence():
    s = run(tb(retained_earnings=(15_000_000, 15_000_000)))
    assert "do not tie" in s.reason


def test_every_check_explains_what_it_means():
    for c in run().checks:
        assert len(c.meaning) > 30, c.name


def test_an_unmapped_account_is_named_rather_than_quietly_dropped():
    rows = tb() + [Account("DX", "9999", "Suspense", "mystery", 0, 500_000)]
    assert unknown_lines(rows) == ["mystery"]
    # and it is excluded from the statements rather than guessed into one
    s = run(rows)
    assert s.revenue == 48_000_000


def test_accounts_are_mapped_explicitly_not_guessed_from_their_names():
    """"Insurance" could be an expense or a prepayment. A product that
    guesses puts the difference in profit."""
    as_expense = run(tb() + [Account("DX", "6300", "Insurance", "opex", 0, 500_000)])
    as_asset = run(tb() + [Account("DX", "1300", "Insurance", "other_current_assets",
                                   0, 500_000)])
    assert as_expense.profit_after_tax < as_asset.profit_after_tax


def test_empty_input_does_not_pretend_to_have_statements():
    s = run([])
    assert s.revenue == 0
    assert "No trial balance" in s.reason
