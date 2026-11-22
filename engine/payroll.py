"""Pakistani payroll.

Separate from the modules because this is not a judgement, it is the law.
Every rate here is a statutory figure with a date and a source, and when the
next Finance Act lands the change is a new row in a table rather than an edit
to a formula. A payroll that is quietly a year out of date is worse than one
that refuses to run.

What is modelled, and what is not:

  Modelled. Salaried income tax under the slabs, deducted monthly as a
  twelfth of the annual liability, which is how employers withhold here.
  EOBI, which is charged on the provincial minimum wage rather than on the
  employee's actual pay. Provincial social security, employer only. Provident
  fund at whatever the employer's scheme says. Gratuity accrued at one
  month's basic a year, which is the common arrangement where there is no
  funded scheme.

  Not modelled, and refused rather than guessed: tax credits for pension
  contributions and charitable donations, the separate treatment of bonuses
  and arrears for the year they relate to, final settlements, and anything
  for non-salaried individuals or an AOP, which keep a surcharge that salary
  no longer has. These need facts this product does not hold, and inventing
  them would put a wrong number on somebody's payslip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence


# ---------------------------------------------------------------- the law


@dataclass(frozen=True)
class Band:
    """One slab: tax is `fixed` plus `rate` of whatever is above `floor`."""

    floor: float
    fixed: float
    rate: float


@dataclass(frozen=True)
class TaxTable:
    """The salaried slabs for one tax year, with where they came from."""

    name: str
    effective_from: date
    effective_to: date
    bands: tuple[Band, ...]
    source: str
    surcharge_rate: float = 0.0
    surcharge_over: float = 0.0


# Tax year 2027: 1 July 2026 to 30 June 2027, under the Finance Act 2026,
# enacted 27 June 2026 and effective 1 July 2026. Eight bands, nil up to
# PKR 600,000, 35% above PKR 7,000,000.
#
# The 9% surcharge on salary above PKR 10,000,000 (section 4AB) was abolished
# for salaried individuals by that Act. It still applies to non-salaried
# individuals and to associations of persons, neither of which this module
# computes, so surcharge_rate is zero here and is not a stand-in for "none
# exists anywhere".
TY2027 = TaxTable(
    name="Tax year 2027 (1 Jul 2026 - 30 Jun 2027)",
    effective_from=date(2026, 7, 1),
    effective_to=date(2027, 6, 30),
    source=(
        "Finance Act 2026, gazetted 26 June 2026, effective 1 July 2026. "
        "Slabs cross-checked against PwC Worldwide Tax Summaries (Pakistan, "
        "reviewed 24 August 2026) and the KPMG Taseer Hadi brief of July 2026."
    ),
    bands=(
        Band(floor=0, fixed=0, rate=0.00),
        Band(floor=600_000, fixed=0, rate=0.01),
        Band(floor=1_200_000, fixed=6_000, rate=0.11),
        Band(floor=2_200_000, fixed=116_000, rate=0.20),
        Band(floor=3_200_000, fixed=316_000, rate=0.25),
        Band(floor=4_100_000, fixed=541_000, rate=0.29),
        Band(floor=5_600_000, fixed=976_000, rate=0.32),
        Band(floor=7_000_000, fixed=1_424_000, rate=0.35),
    ),
)

TABLES = (TY2027,)


def table_for(as_of: date) -> TaxTable:
    """The slabs in force on a date, or a refusal.

    Running last year's slabs against this year's payroll silently understates
    or overstates every payslip, so a date the product has no table for is an
    error rather than a fallback to the newest one it happens to know.
    """
    for t in TABLES:
        if t.effective_from <= as_of <= t.effective_to:
            return t
    known = ", ".join(t.name for t in TABLES)
    raise ValueError(
        f"No salary tax table is held for {as_of.isoformat()}. "
        f"This product knows: {known}. Pakistani slabs change with each "
        f"Finance Act, so the table has to be added rather than assumed."
    )


def annual_tax(taxable_annual: float, table: TaxTable) -> float:
    """Slab tax on an annual taxable salary."""
    if taxable_annual <= 0:
        return 0.0
    band = table.bands[0]
    for b in table.bands:
        if taxable_annual > b.floor:
            band = b
        else:
            break
    tax = band.fixed + (taxable_annual - band.floor) * band.rate
    if table.surcharge_rate and taxable_annual > table.surcharge_over:
        tax += tax * table.surcharge_rate
    return round(tax, 2)


# ------------------------------------------------------- the other levies


@dataclass(frozen=True)
class Statutory:
    """Everything outside the slabs, all of it provincial or scheme-level.

    The minimum wage is provincial and is reset each year, and EOBI is charged
    on it rather than on pay, so one wrong figure here moves every employee's
    contribution at once. It is a setting, not a constant.
    """

    # Sindh's notified figure, which is the highest of the five and so the
    # safe default: it over-states EOBI rather than under-stating it. The
    # platform always passes this in from Thresholds, which carries the same
    # number, so the two cannot drift. MINIMUM_WAGE_2026 below has the rest.
    minimum_wage_monthly: float = 43_000.0
    eobi_employer_rate: float = 0.05
    eobi_employee_rate: float = 0.01
    social_security_rate: float = 0.06
    # Provinces insure workers up to a wage ceiling; above it the employer
    # owes nothing. Zero means "no ceiling applied", which over-states the
    # cost rather than under-stating it.
    social_security_wage_ceiling: float = 0.0
    # One month's basic a year is the usual arrangement where there is no
    # funded scheme. Accrued monthly so the cost lands in the month it was
    # earned rather than as a lump in the month somebody leaves.
    gratuity_months_per_year: float = 1.0
    # Medical allowance is exempt up to a tenth of basic pay.
    medical_exempt_share_of_basic: float = 0.10


# Notified monthly minimum wages seen for 2026-27. Provincial, and they move:
# these are defaults for the settings form, not facts the engine relies on.
MINIMUM_WAGE_2026 = {
    "Sindh": 43_000.0,
    "Islamabad": 40_700.0,
    "Punjab": 40_000.0,
    "Khyber Pakhtunkhwa": 40_000.0,
    "Balochistan": 40_000.0,
}


# ------------------------------------------------------------- the payslip


@dataclass(frozen=True)
class Employee:
    employee_id: str
    name: str
    entity_id: str
    location_id: str
    department: str = ""
    basic_monthly: float = 0.0
    allowances_monthly: float = 0.0        # taxable: house rent, conveyance
    medical_monthly: float = 0.0           # exempt up to a tenth of basic
    employer_pf_rate: float = 0.0
    employee_pf_rate: float = 0.0
    joined: date | None = None
    left: date | None = None
    eobi_registered: bool = True
    social_security: bool = False
    is_active: bool = True


@dataclass(frozen=True)
class Payslip:
    employee_id: str
    name: str
    entity_id: str
    location_id: str
    department: str
    gross_monthly: float
    taxable_annual: float
    exempt_medical_monthly: float
    tax_monthly: float
    eobi_employee: float
    pf_employee: float
    net_monthly: float
    eobi_employer: float
    social_security_employer: float
    pf_employer: float
    gratuity_accrual: float
    employer_cost: float
    effective_tax_rate: float
    reason: str


@dataclass(frozen=True)
class PayrollResult:
    slips: tuple[Payslip, ...]
    headcount: int
    gross_monthly: float
    tax_monthly: float
    eobi_total: float
    social_security_total: float
    pf_total: float
    gratuity_total: float
    net_monthly: float
    employer_cost_monthly: float
    employer_cost_annual: float
    by_department: dict
    table_name: str
    table_source: str
    reason: str
    caveat: str


def _employed(e: Employee, as_of: date) -> bool:
    if not e.is_active:
        return False
    if e.joined and e.joined > as_of:
        return False
    if e.left and e.left < as_of:
        return False
    return True


def payslip(e: Employee, table: TaxTable, rules: Statutory) -> Payslip:
    exempt = min(e.medical_monthly,
                 e.basic_monthly * rules.medical_exempt_share_of_basic)
    taxable_monthly = e.basic_monthly + e.allowances_monthly + (
        e.medical_monthly - exempt)
    taxable_annual = taxable_monthly * 12
    tax_year = annual_tax(taxable_annual, table)
    tax_month = round(tax_year / 12, 2)

    gross = e.basic_monthly + e.allowances_monthly + e.medical_monthly

    # EOBI is charged on the minimum wage, not on pay: the clerk and the
    # country manager generate the same contribution.
    eobi_e = round(rules.minimum_wage_monthly * rules.eobi_employee_rate, 2) \
        if e.eobi_registered else 0.0
    eobi_r = round(rules.minimum_wage_monthly * rules.eobi_employer_rate, 2) \
        if e.eobi_registered else 0.0

    insured = e.social_security and (
        rules.social_security_wage_ceiling <= 0
        or gross <= rules.social_security_wage_ceiling)
    ss = round(gross * rules.social_security_rate, 2) if insured else 0.0

    pf_e = round(e.basic_monthly * e.employee_pf_rate, 2)
    pf_r = round(e.basic_monthly * e.employer_pf_rate, 2)
    gratuity = round(
        e.basic_monthly * rules.gratuity_months_per_year / 12, 2)

    net = round(gross - tax_month - eobi_e - pf_e, 2)
    cost = round(gross + eobi_r + ss + pf_r + gratuity, 2)

    bits = []
    if tax_month:
        bits.append(
            f"{_money(taxable_annual)} taxable a year puts this salary in the "
            f"{int(_band_rate(taxable_annual, table) * 100)}% band")
    else:
        bits.append("below the taxable threshold")
    if exempt:
        bits.append(f"{_money(exempt)} of medical allowance is exempt")
    if eobi_r:
        bits.append("EOBI is charged on the minimum wage, not on this salary")
    if ss:
        bits.append("insured for provincial social security")

    return Payslip(
        employee_id=e.employee_id, name=e.name, entity_id=e.entity_id,
        location_id=e.location_id, department=e.department,
        gross_monthly=round(gross, 2), taxable_annual=round(taxable_annual, 2),
        exempt_medical_monthly=round(exempt, 2), tax_monthly=tax_month,
        eobi_employee=eobi_e, pf_employee=pf_e, net_monthly=net,
        eobi_employer=eobi_r, social_security_employer=ss, pf_employer=pf_r,
        gratuity_accrual=gratuity, employer_cost=cost,
        effective_tax_rate=round(tax_year / taxable_annual, 4)
            if taxable_annual > 0 else 0.0,
        reason="; ".join(bits) + ".",
    )


def _band_rate(taxable_annual: float, table: TaxTable) -> float:
    rate = 0.0
    for b in table.bands:
        if taxable_annual > b.floor:
            rate = b.rate
    return rate


def _money(v: float) -> str:
    """One formatter, so a figure reads the same everywhere.

    This was a private copy of engine/fmt.money with the scale
    hard-coded to millions -- one of four such copies, which is
    how the same amount came to appear in two notations on one
    screen.
    """
    from engine.fmt import money
    return money(v)


def evaluate(
    employees: Sequence[Employee],
    rules: Statutory | None = None,
    as_of: date | None = None,
) -> PayrollResult:
    rules = rules or Statutory()
    as_of = as_of or date.today()
    table = table_for(as_of)

    live = [e for e in employees if _employed(e, as_of)]
    slips = tuple(payslip(e, table, rules) for e in live)

    def total(f):
        return round(sum(getattr(s, f) for s in slips), 2)

    by_department: dict[str, dict] = {}
    for s in slips:
        d = by_department.setdefault(
            s.department or "Unassigned",
            {"headcount": 0, "gross": 0.0, "cost": 0.0})
        d["headcount"] += 1
        d["gross"] = round(d["gross"] + s.gross_monthly, 2)
        d["cost"] = round(d["cost"] + s.employer_cost, 2)

    gross = total("gross_monthly")
    cost = total("employer_cost")
    loading = (cost / gross - 1) if gross else 0.0

    reason = (
        f"{len(slips)} on the payroll cost {_money(cost)} a month, "
        f"{_money(cost * 12)} a year. Gross pay is {_money(gross)}; the rest "
        f"is {loading * 100:.1f}% on top in contributions, provident fund and "
        f"accrued gratuity."
    ) if slips else "Nobody is on the payroll at this date."

    return PayrollResult(
        slips=slips, headcount=len(slips), gross_monthly=gross,
        tax_monthly=total("tax_monthly"),
        eobi_total=round(total("eobi_employee") + total("eobi_employer"), 2),
        social_security_total=total("social_security_employer"),
        pf_total=round(total("pf_employee") + total("pf_employer"), 2),
        gratuity_total=total("gratuity_accrual"),
        net_monthly=total("net_monthly"),
        employer_cost_monthly=cost, employer_cost_annual=round(cost * 12, 2),
        by_department=by_department,
        table_name=table.name, table_source=table.source,
        reason=reason,
        caveat=(
            "Tax is withheld as a twelfth of the annual liability, which is "
            "how it is deducted in practice. Bonuses, arrears, final "
            "settlements and tax credits for pension or charitable "
            "contributions are not modelled, so a payslip with any of those "
            "needs the figure checked against the return."
        ),
    )
