"""Parsing canonical CSV text into records, with rejections kept.

One parser per dataset, all of them with the same contract:

    parse_<dataset>(text) -> (records, rejections)

Nothing is silently dropped. A row that cannot be used comes back in
`rejections` with its line number and the reason, so the data quality panel
can show the customer exactly which rows the engine refused and why. A file
that is 95% usable should produce 95% of an answer and a list of the rest,
not an error page.

This is still the pre-ingestion layer: it reads canonical column names. The
mapping layer that turns an arbitrary Cin7, Odoo or Tally export into these
columns sits in front of it and is a separate piece of work. What makes that
safe to build later is that these parsers and the downloadable templates are
generated from the same registry in engine/samples.py, so the target shape is
defined in exactly one place.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Tuple

from engine.schema import (
    Commitment,
    CommitmentKind,
    DemandPeriod,
    ItemSnapshot,
    MachinePlacement,
    ReceivableInvoice,
    ServiceJob,
)

Rejection = Tuple[int, str, dict]
TRUTHY = ("1", "true", "yes", "y", "t")


class RowError(ValueError):
    """A row that cannot be used. The message is shown to the customer."""


# ------------------------------------------------------------- field readers


def _s(row: dict, key: str, default: str = "") -> str:
    return (row.get(key) or "").strip() or default


def _req(row: dict, key: str) -> str:
    v = _s(row, key)
    if not v:
        raise RowError(f"{key} is empty")
    return v


def _f(row: dict, key: str, default: float = 0.0) -> float:
    raw = _s(row, key)
    if not raw:
        return default
    # Accept thousands separators and a currency prefix, which survive most
    # Excel round trips. Anything else is reported rather than guessed.
    cleaned = raw.replace(",", "").replace("PKR", "").replace("Rs", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        raise RowError(f"{key} is not a number: {raw!r}")


def _nonneg(row: dict, key: str, default: float = 0.0) -> float:
    v = _f(row, key, default)
    if v < 0:
        raise RowError(f"{key} is negative: {v}")
    return v


def _b(row: dict, key: str) -> bool:
    return _s(row, key).lower() in TRUTHY


def _date(row: dict, key: str, required: bool = False) -> Optional[date]:
    raw = _s(row, key)
    if not raw:
        if required:
            raise RowError(f"{key} is empty")
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    # Deliberately not guessing between DD/MM and MM/DD beyond the orders
    # tried above. A silently wrong date is worse than a rejected row.
    raise RowError(f"{key} is not a date the engine recognises: {raw!r}")


def _ent(row: dict) -> str:
    return _s(row, "entity_id", "default")


def _loc(row: dict) -> str:
    return _s(row, "location_id", "default")


# ------------------------------------------------------------- row builders


def _item(row: dict) -> ItemSnapshot:
    return ItemSnapshot(
        item_key=_req(row, "item_key"),
        description=_s(row, "description"),
        category=_s(row, "category"),
        on_hand=_nonneg(row, "on_hand"),
        open_po=_nonneg(row, "open_po"),
        unit_cost=_nonneg(row, "unit_cost"),
        lead_time_days=_nonneg(row, "lead_time_days"),
        lead_time_days_std=_nonneg(row, "lead_time_days_std"),
        is_clinical_critical=_b(row, "is_clinical_critical"),
        expiry_date=_date(row, "expiry_date"),
        entity_id=_ent(row),
        location_id=_loc(row),
    )


def _period(row: dict) -> DemandPeriod:
    period = _req(row, "period")
    if len(period) != 7 or period[4] != "-":
        raise RowError(f"period must be YYYY-MM, got {period!r}")
    return DemandPeriod(
        item_key=_req(row, "item_key"),
        period=period,
        quantity=_nonneg(row, "quantity"),
        entity_id=_ent(row),
        location_id=_loc(row),
    )


def _invoice(row: dict) -> ReceivableInvoice:
    amount = _nonneg(row, "amount")
    paid = _nonneg(row, "amount_paid")
    if paid > amount:
        raise RowError(f"amount_paid {paid} exceeds amount {amount}")
    return ReceivableInvoice(
        invoice_no=_req(row, "invoice_no"),
        customer_key=_req(row, "customer_key"),
        customer_name=_s(row, "customer_name"),
        invoice_date=_date(row, "invoice_date", required=True),
        due_date=_date(row, "due_date", required=True),
        amount=amount,
        amount_paid=paid,
        is_public_sector=_b(row, "is_public_sector"),
        is_intercompany=_b(row, "is_intercompany"),
        entity_id=_ent(row),
        location_id=_loc(row),
    )


def _job(row: dict) -> ServiceJob:
    opened = _date(row, "opened_date", required=True)
    closed = _date(row, "closed_date")
    if closed and closed < opened:
        raise RowError("closed_date is before opened_date")
    return ServiceJob(
        job_no=_req(row, "job_no"),
        machine_key=_req(row, "machine_key"),
        opened_date=opened,
        closed_date=closed,
        labour_hours=_nonneg(row, "labour_hours"),
        labour_hours_billed=_nonneg(row, "labour_hours_billed"),
        parts_cost=_nonneg(row, "parts_cost"),
        parts_charged=_nonneg(row, "parts_charged"),
        is_warranty=_b(row, "is_warranty"),
        is_repeat_visit=_b(row, "is_repeat_visit"),
        downtime_days=_nonneg(row, "downtime_days"),
        entity_id=_ent(row),
        location_id=_loc(row),
    )


def _placement(row: dict) -> MachinePlacement:
    margin = _f(row, "reagent_margin_pct", 0.35)
    if margin > 1:
        # 35 almost certainly means 35%, but acting on that guess would change
        # every payback figure, so it is refused rather than converted.
        raise RowError(
            f"reagent_margin_pct must be a decimal such as 0.35, got {margin}"
        )
    return MachinePlacement(
        machine_key=_req(row, "machine_key"),
        model=_s(row, "model"),
        customer_key=_req(row, "customer_key"),
        customer_name=_s(row, "customer_name"),
        placed_date=_date(row, "placed_date", required=True),
        capital_cost=_nonneg(row, "capital_cost"),
        contract_months=int(_nonneg(row, "contract_months")),
        reagent_revenue_to_date=_nonneg(row, "reagent_revenue_to_date"),
        reagent_margin_pct=margin,
        expected_monthly_tests=_nonneg(row, "expected_monthly_tests"),
        actual_monthly_tests=_nonneg(row, "actual_monthly_tests"),
        downtime_days_ytd=_nonneg(row, "downtime_days_ytd"),
        service_cost_to_date=_nonneg(row, "service_cost_to_date"),
        revenue_per_test=_nonneg(row, "revenue_per_test"),
        entity_id=_ent(row),
        location_id=_loc(row),
    )


def _commitment(row: dict) -> Commitment:
    raw = _s(row, "kind", "other").lower().replace(" ", "_")
    try:
        kind = CommitmentKind(raw)
    except ValueError:
        # An unknown kind is not worth rejecting a payment over: the kind
        # explains a cash week, it does not take part in the arithmetic.
        kind = CommitmentKind.OTHER
    every = int(_nonneg(row, "recurs_every_days"))
    recurring = _b(row, "is_recurring")
    if recurring and every <= 0:
        raise RowError("is_recurring is yes but recurs_every_days is not set")
    return Commitment(
        label=_req(row, "label"),
        kind=kind,
        due_date=_date(row, "due_date", required=True),
        amount=_nonneg(row, "amount"),
        is_recurring=recurring,
        recurs_every_days=every,
        entity_id=_ent(row),
        location_id=_loc(row),
    )


def _account(row: dict):
    from engine.modules.finance import ALL_LINES, Account

    line = _req(row, "statement_line").strip().lower().replace(" ", "_")
    if line not in ALL_LINES:
        # An account routed nowhere would silently drop out of the
        # statements, and the profit would be wrong by exactly that amount.
        raise RowError(
            f"statement_line {line!r} is not one the statements use. "
            f"It has to be one of: {', '.join(ALL_LINES)}"
        )
    return Account(
        entity_id=_ent(row),
        code=_req(row, "account_code"),
        name=_s(row, "account_name") or _req(row, "account_code"),
        line=line,
        opening=_f(row, "opening_balance"),
        closing=_f(row, "closing_balance"),
    )


def _employee(row: dict):
    from engine.payroll import Employee

    basic = _nonneg(row, "basic_monthly")
    if basic <= 0:
        raise RowError("basic_monthly has to be more than zero")
    joined = _date(row, "joined")
    left = _date(row, "left")
    if joined and left and left < joined:
        raise RowError("left is before joined")
    for field in ("employer_pf_rate", "employee_pf_rate"):
        rate = _nonneg(row, field)
        if rate > 1:
            raise RowError(
                f"{field} is {rate}, which reads as a percentage. Use a "
                f"decimal: 0.08 for 8%."
            )
    return Employee(
        employee_id=_req(row, "employee_id"),
        name=_s(row, "name") or _req(row, "employee_id"),
        entity_id=_ent(row),
        location_id=_loc(row),
        department=_s(row, "department"),
        basic_monthly=basic,
        allowances_monthly=_nonneg(row, "allowances_monthly"),
        medical_monthly=_nonneg(row, "medical_monthly"),
        employer_pf_rate=_nonneg(row, "employer_pf_rate"),
        employee_pf_rate=_nonneg(row, "employee_pf_rate"),
        joined=joined,
        left=left,
        eobi_registered=_b(row, "eobi_registered") or not _s(row, "eobi_registered"),
        social_security=_b(row, "social_security"),
        is_active=_b(row, "is_active") or not _s(row, "is_active"),
    )


def _opportunity(row: dict):
    from engine.modules.marketing import STAGES, Opportunity

    stage = _req(row, "stage").strip().lower().replace(" ", "_")
    if stage not in STAGES:
        raise RowError(
            f"stage {stage!r} is not one of: {', '.join(STAGES)}"
        )
    return Opportunity(
        opportunity_id=_req(row, "opportunity_id"),
        customer_key=_req(row, "customer_key"),
        customer_name=_s(row, "customer_name") or _req(row, "customer_key"),
        entity_id=_ent(row),
        location_id=_loc(row),
        stage=stage,
        value=_nonneg(row, "value"),
        expected_close=_date(row, "expected_close"),
        item_key=_s(row, "item_key"),
        quantity=_nonneg(row, "quantity"),
        source=_s(row, "source").strip().lower(),
        owner=_s(row, "owner"),
        opened=_date(row, "opened"),
        closed=_date(row, "closed"),
    )


def _spend(row: dict):
    from engine.modules.marketing import Spend

    return Spend(
        entity_id=_ent(row),
        label=_req(row, "label"),
        category=_s(row, "category").strip().lower() or "other",
        spent_on=_date(row, "spent_on", required=True),
        amount=_nonneg(row, "amount"),
    )


BUILDERS: Dict[str, Callable[[dict], object]] = {
    "inventory": _item,
    "demand": _period,
    "receivables": _invoice,
    "service": _job,
    "machines": _placement,
    "commitments": _commitment,
    "trial_balance": _account,
    "employees": _employee,
    "opportunities": _opportunity,
    "marketing_spend": _spend,
}


# ------------------------------------------------------------------- driver


def parse(dataset_id: str, text: str) -> Tuple[List[object], List[Rejection]]:
    """Parse canonical CSV text. Returns (records, rejections)."""
    build = BUILDERS[dataset_id]
    records: List[object] = []
    rejected: List[Rejection] = []

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return [], [(1, "the file is empty", {})]

    # Line 1 is the header, so data starts at line 2.
    for n, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue  # blank line, common at the end of an Excel export
        try:
            records.append(build(row))
        except RowError as exc:
            rejected.append((n, str(exc), row))
        except Exception as exc:  # malformed row shape, not a field problem
            rejected.append((n, f"{type(exc).__name__}: {exc}", row))

    return records, rejected


def missing_columns(dataset_id: str, text: str) -> List[str]:
    """Required columns absent from the header, for a clear up-front message."""
    from engine.samples import DATASETS

    reader = csv.DictReader(io.StringIO(text))
    present = set(reader.fieldnames or [])
    return [
        c.name
        for c in DATASETS[dataset_id].columns
        if c.required and c.name not in present
    ]
