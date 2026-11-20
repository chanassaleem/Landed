"""Pakistani payroll.

Every expected figure here is computed by hand from the Finance Act 2026
slabs, not from the code. A payroll test that asserts what the code already
does proves nothing.
"""

from datetime import date

import pytest

from engine.payroll import (
    Employee, Statutory, TY2027, annual_tax, evaluate, payslip, table_for,
)


# ---------------------------------------------------------------- the slabs

@pytest.mark.parametrize("taxable,expected", [
    (0, 0),
    (600_000, 0),                                    # the threshold itself
    (600_001, 0.01),                                 # a rupee over
    (1_200_000, 6_000),                              # top of the 1% band
    (1_800_000, 72_000),                             # 6,000 + 11% of 600,000
    (2_200_000, 116_000),
    (3_200_000, 316_000),
    (4_100_000, 541_000),
    (4_800_000, 744_000),                            # 541,000 + 29% of 700,000
    (5_600_000, 976_000),
    (7_000_000, 1_424_000),
    (15_000_000, 4_224_000),                         # 1,424,000 + 35% of 8M
])
def test_the_slabs_match_the_finance_act(taxable, expected):
    assert annual_tax(taxable, TY2027) == pytest.approx(expected, abs=0.5)


def test_the_bands_are_continuous():
    """No step at a boundary: a rupee more of salary must never cost
    thousands more in tax."""
    for band in TY2027.bands[1:]:
        just_under = annual_tax(band.floor - 1, TY2027)
        at_floor = annual_tax(band.floor, TY2027)
        assert at_floor - just_under < 1.0, band


def test_the_salaried_surcharge_is_gone():
    """Abolished for salaried individuals by the Finance Act 2026. It still
    applies to non-salaried individuals and AOPs, which this does not do."""
    assert TY2027.surcharge_rate == 0.0
    assert annual_tax(12_000_000, TY2027) == pytest.approx(
        1_424_000 + 0.35 * 5_000_000, abs=0.5)


def test_a_date_with_no_table_is_refused_not_guessed():
    """Running last year's slabs silently is worse than refusing."""
    with pytest.raises(ValueError, match="No salary tax table"):
        table_for(date(2024, 1, 1))
    assert table_for(date(2026, 11, 20)) is TY2027
    assert table_for(date(2026, 7, 1)) is TY2027
    assert table_for(date(2027, 6, 30)) is TY2027


def test_every_rate_in_the_table_carries_its_source():
    assert "Finance Act 2026" in TY2027.source
    assert TY2027.effective_from == date(2026, 7, 1)


# -------------------------------------------------------------- a payslip

RULES = Statutory(minimum_wage_monthly=43_000)       # Sindh, 2026-27


def test_a_manager_on_192k_a_month():
    """Hand-worked:
        basic 120,000 + allowances 60,000 + medical 12,000 = 192,000 gross
        medical exempt = min(12,000, 10% of 120,000) = 12,000, so all of it
        taxable monthly = 180,000, annual 2,160,000
        tax = 6,000 + 11% x 960,000 = 111,600 a year, 9,300 a month
        EOBI employee = 1% x 43,000 = 430
        PF employee = 8% x 120,000 = 9,600
        net = 192,000 - 9,300 - 430 - 9,600 = 172,670
        employer = 192,000 + 2,150 EOBI + 9,600 PF + 10,000 gratuity
                 = 213,750
    """
    e = Employee("E1", "Ayesha", "DX", "KHI", "Sales",
                 basic_monthly=120_000, allowances_monthly=60_000,
                 medical_monthly=12_000, employer_pf_rate=0.08,
                 employee_pf_rate=0.08)
    s = payslip(e, TY2027, RULES)
    assert s.gross_monthly == 192_000
    assert s.exempt_medical_monthly == 12_000
    assert s.taxable_annual == 2_160_000
    assert s.tax_monthly == pytest.approx(9_300, abs=1)
    assert s.eobi_employee == 430
    assert s.pf_employee == 9_600
    assert s.net_monthly == pytest.approx(172_670, abs=1)
    assert s.employer_cost == pytest.approx(213_750, abs=1)


def test_a_warehouseman_below_the_threshold_pays_no_tax():
    e = Employee("E2", "Bilal", "DX", "KHI", "Warehouse",
                 basic_monthly=30_000, allowances_monthly=12_000,
                 medical_monthly=3_000, social_security=True)
    s = payslip(e, TY2027, RULES)
    assert s.taxable_annual == 504_000
    assert s.tax_monthly == 0
    # 6% of gross, employer only
    assert s.social_security_employer == pytest.approx(45_000 * 0.06, abs=1)
    assert s.net_monthly == pytest.approx(45_000 - 430, abs=1)


def test_eobi_is_charged_on_the_minimum_wage_not_on_pay():
    """The clerk and the country manager generate the same contribution.
    Charging it on salary would overstate the cost of senior staff several
    times over."""
    low = payslip(Employee("A", "A", "DX", "KHI", basic_monthly=30_000),
                  TY2027, RULES)
    high = payslip(Employee("B", "B", "DX", "KHI", basic_monthly=500_000),
                   TY2027, RULES)
    assert low.eobi_employer == high.eobi_employer == 43_000 * 0.05
    assert low.eobi_employee == high.eobi_employee == 430


def test_the_minimum_wage_is_a_setting_because_it_is_provincial():
    sindh = payslip(Employee("A", "A", "DX", "KHI", basic_monthly=50_000),
                    TY2027, Statutory(minimum_wage_monthly=43_000))
    islamabad = payslip(Employee("A", "A", "DX", "ISB", basic_monthly=50_000),
                        TY2027, Statutory(minimum_wage_monthly=40_700))
    assert sindh.eobi_employer > islamabad.eobi_employer


def test_social_security_stops_at_the_ceiling_when_one_is_set():
    rules = Statutory(minimum_wage_monthly=43_000,
                      social_security_wage_ceiling=50_000)
    under = payslip(Employee("A", "A", "DX", "KHI", basic_monthly=40_000,
                             social_security=True), TY2027, rules)
    over = payslip(Employee("B", "B", "DX", "KHI", basic_monthly=90_000,
                            social_security=True), TY2027, rules)
    assert under.social_security_employer > 0
    assert over.social_security_employer == 0


def test_medical_exemption_is_capped_at_a_tenth_of_basic():
    e = Employee("E", "E", "DX", "KHI", basic_monthly=100_000,
                 medical_monthly=30_000)
    s = payslip(e, TY2027, RULES)
    assert s.exempt_medical_monthly == 10_000
    # the other 20,000 is taxable
    assert s.taxable_annual == (100_000 + 20_000) * 12


def test_gratuity_accrues_monthly_rather_than_landing_in_one_month():
    e = Employee("E", "E", "DX", "KHI", basic_monthly=120_000)
    s = payslip(e, TY2027, RULES)
    assert s.gratuity_accrual == pytest.approx(10_000, abs=1)


# ----------------------------------------------------------------- the run

def test_leavers_and_joiners_are_not_paid_outside_their_dates():
    emps = [
        Employee("A", "A", "DX", "KHI", basic_monthly=50_000,
                 joined=date(2026, 1, 1)),
        Employee("B", "B", "DX", "KHI", basic_monthly=50_000,
                 joined=date(2027, 1, 1)),               # not started
        Employee("C", "C", "DX", "KHI", basic_monthly=50_000,
                 left=date(2026, 6, 30)),                # gone
        Employee("D", "D", "DX", "KHI", basic_monthly=50_000,
                 is_active=False),
    ]
    r = evaluate(emps, RULES, date(2026, 11, 20))
    assert r.headcount == 1
    assert [s.employee_id for s in r.slips] == ["A"]


def test_the_run_totals_what_the_payslips_say():
    emps = [
        Employee("A", "A", "DX", "KHI", "Sales", basic_monthly=120_000,
                 allowances_monthly=60_000, employer_pf_rate=0.08,
                 employee_pf_rate=0.08),
        Employee("B", "B", "DX", "LHR", "Warehouse", basic_monthly=30_000,
                 social_security=True),
    ]
    r = evaluate(emps, RULES, date(2026, 11, 20))
    assert r.gross_monthly == pytest.approx(
        sum(s.gross_monthly for s in r.slips), abs=0.5)
    assert r.employer_cost_monthly == pytest.approx(
        sum(s.employer_cost for s in r.slips), abs=0.5)
    assert r.employer_cost_annual == pytest.approx(
        r.employer_cost_monthly * 12, abs=0.5)
    assert r.employer_cost_monthly > r.gross_monthly, \
        "employing somebody costs more than their pay"


def test_departments_are_totalled_for_the_profit_and_loss():
    emps = [
        Employee("A", "A", "DX", "KHI", "Sales", basic_monthly=100_000),
        Employee("B", "B", "DX", "KHI", "Sales", basic_monthly=80_000),
        Employee("C", "C", "DX", "KHI", "Service", basic_monthly=60_000),
    ]
    r = evaluate(emps, RULES, date(2026, 11, 20))
    assert r.by_department["Sales"]["headcount"] == 2
    assert r.by_department["Service"]["headcount"] == 1


def test_the_result_names_the_tax_year_it_used():
    r = evaluate([Employee("A", "A", "DX", "KHI", basic_monthly=50_000)],
                 RULES, date(2026, 11, 20))
    assert "2027" in r.table_name
    assert "Finance Act 2026" in r.table_source


def test_what_is_not_modelled_is_said_out_loud():
    r = evaluate([Employee("A", "A", "DX", "KHI", basic_monthly=50_000)],
                 RULES, date(2026, 11, 20))
    for missing in ("Bonuses", "arrears", "tax credits"):
        assert missing.lower() in r.caveat.lower()
