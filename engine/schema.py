"""Canonical data model for the S&OP engine.

Everything entering the engine is normalised into these records first.
Source files never reach the calculation modules.

Units are explicit and never implied:
  quantities  -> units of the item's canonical UOM
  money       -> PKR
  demand      -> units per calendar month
  lead time   -> calendar days
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------- enums


class DemandClass(str, Enum):
    """Syntetos-Boylan-Croston classification by ADI and CV squared."""

    SMOOTH = "smooth"
    ERRATIC = "erratic"
    INTERMITTENT = "intermittent"
    LUMPY = "lumpy"
    NO_HISTORY = "no_history"


class AbcClass(str, Enum):
    A = "A"
    B = "B"
    C = "C"


class ServiceMetric(str, Enum):
    """Which service measure the target applies to.

    Fill rate suits steady demand under continuous review.
    Cycle service level suits intermittent and lumpy demand.
    An 80% fill rate and a 0% cycle service level can describe the same
    stockout, so the two are never compared directly.
    """

    FILL_RATE = "fill_rate"
    CYCLE_SERVICE_LEVEL = "cycle_service_level"


class StockStatus(str, Enum):
    CRITICAL = "CRITICAL"
    REORDER = "REORDER"
    EXCESS = "EXCESS"
    EXPIRY_RISK = "EXPIRY_RISK"
    OK = "OK"


# ---------------------------------------------------------------- inputs


@dataclass(frozen=True)
class Thresholds:
    """Per-entity configuration. Every value here is customer-settable.

    No threshold is hard-coded anywhere else in the engine.
    """

    target_cover_months: float = 3.0
    excess_cover_months: float = 6.0
    cost_of_capital_annual: float = 0.22
    target_dso_days: int = 60
    labour_rate_per_hour: float = 2500.0
    payback_target_months: int = 24

    # Redeployment guards. A transfer has handling cost and risk, so trivial
    # moves are not suggested, and a sending location is never stripped below
    # its own cover floor to rescue another.
    min_transfer_value: float = 25_000.0
    sender_min_cover_months: float = 3.0

    # A payback rate extrapolated from a few weeks of reagent pull is noise. A
    # placement is reported as RAMPING until it has been running this long,
    # otherwise every new machine looks like a write-off on day thirty.
    placement_min_months_to_judge: int = 6

    # Module 2. Seasonality needs two full cycles before it is anything but a
    # fit to noise, so 24 monthly observations to estimate 12 monthly indices.
    min_periods_for_seasonality: int = 24
    forecast_horizon_months: int = 6
    holt_alpha: float = 0.3      # level smoothing
    holt_beta: float = 0.1       # trend smoothing
    croston_alpha: float = 0.2   # Croston/SBA smoothing

    # Module 8. Cash the business will not spend down to, so the engine never
    # recommends an order that eats the floor.
    cash_buffer: float = 2_000_000.0
    # Payroll. The minimum wage is provincial and is reset each year, and
    # EOBI is charged on it rather than on pay, so one wrong figure moves
    # every employee's contribution at once. Default is the 2026-27 Sindh
    # notification; change it for the province you employ in.
    minimum_wage_monthly: float = 43_000.0
    # Provinces insure workers up to a wage ceiling. Zero applies no ceiling,
    # which overstates the cost rather than understating it.
    social_security_wage_ceiling: float = 0.0
    cash_horizon_weeks: int = 13
    # An invoice already later than its own customer's average lateness is,
    # by construction, about half the open book. Expecting all of it in the
    # current week front-loads the horizon and flatters week one, so it is
    # spread evenly across this catch-up window instead.
    overdue_catchup_weeks: int = 4

    # How money is written into the sentences the engine produces. Not a
    # display setting: the engine writes "PKR 6.6M is past due" into an
    # insight, and on a Pakistani screen that sits next to a headline
    # reading 66.0L. Same number, two notations, one of them unreadable
    # to the reader it was written for. The browser keeps its own
    # formatter for figures in a column, which is a typesetting problem
    # the server cannot see; this is for money inside a sentence.
    currency: str = "PKR"
    number_scale: str = "indian"

    # Module 9. How long after an order is placed the money actually leaves.
    # In this market a letter of credit is opened at close to the full value
    # before the goods ship, so the cash goes at week zero, not on arrival.
    lc_payment_lag_days: int = 7

    # Service level targets by ABC class and demand class.
    # Clinical criticality overrides these upward, never downward.
    service_targets: dict = field(
        default_factory=lambda: {
            ("A", "smooth"): 0.98,
            ("A", "erratic"): 0.97,
            ("A", "intermittent"): 0.97,
            ("A", "lumpy"): 0.95,
            ("B", "smooth"): 0.95,
            ("B", "erratic"): 0.93,
            ("B", "intermittent"): 0.93,
            ("B", "lumpy"): 0.90,
            ("C", "smooth"): 0.90,
            ("C", "erratic"): 0.88,
            ("C", "intermittent"): 0.88,
            ("C", "lumpy"): 0.85,
        }
    )
    clinical_critical_floor: float = 0.99

    # ABC cut-offs on cumulative share of annual consumption value.
    abc_a_cutoff: float = 0.80
    abc_b_cutoff: float = 0.95


@dataclass(frozen=True)
class ItemSnapshot:
    """One row of an inventory report, after mapping and normalisation."""

    item_key: str
    description: str
    category: str
    on_hand: float
    open_po: float
    unit_cost: float
    lead_time_days: float
    lead_time_days_std: float = 0.0
    is_clinical_critical: bool = False
    expiry_date: Optional[date] = None
    entity_id: str = "default"
    location_id: str = "default"
    # Whether unit_cost rests on a shipment charge that could not be
    # divided properly, and how much of it does. Default False so a
    # snapshot built from a spreadsheet, where there are no shipment
    # charges to be uncertain about, says nothing about it. See 0024.
    cost_provisional: bool = False
    cost_provisional_of: int = 0
    cost_receipts: int = 0
    cost_why: str = ""


@dataclass(frozen=True)
class DemandPeriod:
    """One period of demand history for one item. Zero periods matter."""

    item_key: str
    period: str  # YYYY-MM
    quantity: float
    entity_id: str = "default"
    location_id: str = "default"


# ---------------------------------------------------------------- outputs


@dataclass(frozen=True)
class ClassificationResult:
    item_key: str
    periods_observed: int
    nonzero_periods: int
    adi: Optional[float]
    cv_squared: Optional[float]
    demand_class: DemandClass
    mean_monthly_demand: float
    std_monthly_demand: float
    annual_consumption_value: float
    abc_class: AbcClass
    forecast_method: str
    # Which branch these statistics describe. Demand is classified per
    # stocking location, because each branch reorders for itself: pooling
    # branches would hand every branch the average branch's mean, and would
    # read the level difference between branches as demand variability.
    # Last, with a default, so that every existing construction still works.
    location_id: str = "default"


@dataclass(frozen=True)
class InventoryResult:
    item_key: str
    description: str
    on_hand: float
    open_po: float
    position: float               # on_hand + open_po
    demand_class: DemandClass
    abc_class: AbcClass
    service_target: float
    service_metric: ServiceMetric
    safety_stock: float
    reorder_point: float
    order_up_to: float
    suggested_order_qty: float
    months_cover: Optional[float]  # display only, drives no decision
    status: StockStatus
    stock_value: float
    excess_value: float
    days_to_expiry: Optional[int]
    reason: str                    # plain-language why, for the insight card


# -------------------------------------------------- money, service, equipment
#
# Added for modules 4-7. Same discipline as above: these are the canonical
# records, produced by the mapping layer, never read from source files by the
# calculation modules.


class AgeBucket(str, Enum):
    """Receivable age bands. Public-sector terms in this market routinely
    run past 180 days, so the top band is open-ended rather than '90+'."""

    CURRENT = "current"
    D1_30 = "1-30"
    D31_60 = "31-60"
    D61_90 = "61-90"
    D91_180 = "91-180"
    D180_PLUS = "180+"


class PaybackStatus(str, Enum):
    """Where a placed instrument sits against its capital recovery plan."""

    AHEAD = "AHEAD"
    ON_TRACK = "ON_TRACK"
    BEHIND = "BEHIND"
    RAMPING = "RAMPING"          # too early to judge; inside the observation window
    STALLED = "STALLED"          # no reagent pull in the measured window
    UNRECOVERABLE = "UNRECOVERABLE"  # contract ends before capital is repaid


class TransferUrgency(str, Enum):
    URGENT = "URGENT"    # receiving location is CRITICAL
    USEFUL = "USEFUL"    # receiving location is at or below reorder point
    OPTIONAL = "OPTIONAL"


@dataclass(frozen=True)
class ReceivableInvoice:
    """One open invoice line, after mapping. Paid-in-full rows may be
    supplied; the module nets them out rather than assuming they are absent."""

    invoice_no: str
    customer_key: str
    customer_name: str
    invoice_date: date
    due_date: date
    amount: float
    amount_paid: float = 0.0
    is_public_sector: bool = False
    is_intercompany: bool = False
    entity_id: str = "default"
    location_id: str = "default"


@dataclass(frozen=True)
class ServiceJob:
    """One service or repair job. Wastage is the gap between what was
    consumed and what was billed, which no ERP in this market reports."""

    job_no: str
    machine_key: str
    opened_date: date
    closed_date: Optional[date] = None
    labour_hours: float = 0.0
    labour_hours_billed: float = 0.0
    parts_cost: float = 0.0
    parts_charged: float = 0.0
    is_warranty: bool = False
    is_repeat_visit: bool = False
    downtime_days: float = 0.0
    entity_id: str = "default"
    location_id: str = "default"


@dataclass(frozen=True)
class MachinePlacement:
    """An instrument placed with a customer, recovered through consumable
    margin rather than a sale price. This is the record no off-the-shelf
    system models: capital out, reagent margin back."""

    machine_key: str
    model: str
    customer_key: str
    customer_name: str
    placed_date: date
    capital_cost: float
    contract_months: int
    reagent_revenue_to_date: float = 0.0
    reagent_margin_pct: float = 0.35
    expected_monthly_tests: float = 0.0
    actual_monthly_tests: float = 0.0
    downtime_days_ytd: float = 0.0
    service_cost_to_date: float = 0.0
    revenue_per_test: float = 0.0
    entity_id: str = "default"
    location_id: str = "default"


# ------------------------------------------------------------ module outputs


@dataclass(frozen=True)
class CustomerExposure:
    """Total exposure to one customer across every entity in the group.

    The group figure is the point. A hospital that looks manageable to one
    entity can be several times that across the group, and no entity's own
    ERP can see it.
    """

    customer_key: str
    customer_name: str
    is_public_sector: bool
    entities: tuple
    invoice_count: int
    outstanding: float
    overdue: float
    worst_days_overdue: int
    weighted_avg_days_overdue: float
    buckets: dict
    financing_cost_annual: float
    share_of_book: float
    reason: str


@dataclass(frozen=True)
class ReceivablesResult:
    as_of: date
    outstanding: float
    overdue: float
    intercompany_excluded: float
    dso_days: Optional[float]
    target_dso_days: int
    dso_gap_days: Optional[float]
    cash_released_at_target: float
    buckets: dict
    financing_cost_annual: float
    customers: tuple
    public_sector_share: float


@dataclass(frozen=True)
class ServiceResult:
    as_of: date
    jobs: int
    jobs_open: int
    labour_hours: float
    labour_hours_unbilled: float
    labour_value_lost: float
    parts_cost: float
    parts_uncharged: float
    parts_value_lost: float
    total_value_lost: float
    recovery_rate: Optional[float]
    repeat_visit_rate: Optional[float]
    mean_days_to_close: Optional[float]
    downtime_days: float
    by_machine: tuple
    reason: str


@dataclass(frozen=True)
class MachineResult:
    machine_key: str
    model: str
    customer_name: str
    entity_id: str
    location_id: str
    months_elapsed: float
    contract_months: int
    capital_cost: float
    margin_recovered: float
    service_cost_to_date: float
    net_recovered: float
    recovery_pct: float
    monthly_margin_rate: Optional[float]
    payback_months: Optional[float]
    months_remaining_on_contract: float
    utilisation: Optional[float]
    downtime_days_ytd: float
    revenue_lost_to_downtime: float
    status: PaybackStatus
    reason: str
    # What will still be outstanding when the contract ends, at the current
    # pull. Zero unless the verdict is UNRECOVERABLE. Computed here so the
    # figure on a dashboard tile and the figure in the sentence beneath it
    # cannot disagree.
    unrecoverable_shortfall: float = 0.0


@dataclass(frozen=True)
class TransferSuggestion:
    """Move stock between locations instead of importing it.

    The highest-value insight the engine produces: it releases cash this
    week rather than committing more at a 60-90 day lead time.
    """

    item_key: str
    description: str
    from_entity: str
    from_location: str
    to_entity: str
    to_location: str
    quantity: float
    unit_cost: float
    value_moved: float
    import_avoided: float
    receiving_status: str
    sending_cover_after: Optional[float]
    is_intercompany: bool
    urgency: TransferUrgency
    reason: str


# ------------------------------------------- forecast, cash, sequencing
#
# Added for modules 2, 8 and 9.


class CommitmentKind(str, Enum):
    """What a committed outflow is. The kind decides nothing in the
    arithmetic; it exists so a cash week can be explained."""

    LC_SETTLEMENT = "lc_settlement"
    LOAN_REPAYMENT = "loan_repayment"
    PAYROLL = "payroll"
    RENT = "rent"
    TAX = "tax"
    SUPPLIER = "supplier"
    OTHER = "other"


class OrderVerdict(str, Enum):
    """What module 9 decided about a suggested order."""

    PLACE_NOW = "PLACE_NOW"
    SCHEDULED = "SCHEDULED"       # funded, but in a later week
    TRANSFER_INSTEAD = "TRANSFER_INSTEAD"
    UNFUNDED = "UNFUNDED"         # no week in the horizon can pay for it


@dataclass(frozen=True)
class Commitment:
    """A payment already committed, which cash planning cannot ignore.

    A 13-week cash view built from receivables and purchase orders alone is
    fiction: the letter of credit settles, the bank is repaid and the salaries
    go out whatever the stock position says.
    """

    label: str
    kind: CommitmentKind
    due_date: date
    amount: float
    is_recurring: bool = False
    recurs_every_days: int = 0
    entity_id: str = "default"
    location_id: str = "default"


@dataclass(frozen=True)
class ForecastResult:
    """Module 2 output for one item at one location."""

    item_key: str
    entity_id: str
    location_id: str
    demand_class: DemandClass
    method: str
    periods_observed: int
    mean_monthly_demand: float
    # Point forecast per month ahead, nearest first.
    forecast: tuple
    # Lower and upper bound at the item's service target, same length.
    lower: tuple
    upper: tuple
    level: float                  # smoothed level
    trend: float                  # per month, zero where none is fitted
    seasonality_fitted: bool
    mape: Optional[float]         # on a holdout, None when too little history
    bias: Optional[float]         # mean error, signed: positive is over-forecast
    reason: str
    caveat: str                   # what this forecast cannot be asked to do


@dataclass(frozen=True)
class CashWeek:
    """One week of the 13-week view."""

    index: int                    # 0 is the week containing as_of
    starts: date
    ends: date
    opening: float
    receipts_receivables: float
    receipts_reagent: float
    payments_commitments: float
    payments_orders: float
    payments_service: float
    closing: float
    is_negative: bool
    lines: tuple                  # plain-language explanation of the week


@dataclass(frozen=True)
class CashResult:
    as_of: date
    opening_balance: float
    weeks: tuple
    lowest_balance: float
    lowest_week_index: int
    weeks_negative: int
    total_receipts: float
    total_payments: float
    closing_balance: float
    headroom_for_orders: float    # what the horizon can fund without going under
    reason: str
    caveat: str


@dataclass(frozen=True)
class SequencedOrder:
    """Module 9 output: one suggested order, placed, scheduled or refused."""

    item_key: str
    description: str
    entity_id: str
    location_id: str
    quantity: float
    value: float
    verdict: OrderVerdict
    week_index: Optional[int]
    place_by: Optional[date]      # last date an order still lands before stockout
    arrives: Optional[date]
    days_of_cover_left: Optional[float]
    is_clinical_critical: bool
    abc_class: AbcClass
    priority_score: float
    stockout_cost_per_rupee: float
    covered_by_transfer: float    # units a transfer already covers
    reason: str


@dataclass(frozen=True)
class SequencingResult:
    as_of: date
    horizon_weeks: int
    orders: tuple
    value_requested: float
    value_placed_now: float
    value_scheduled: float
    value_unfunded: float
    value_covered_by_transfer: float
    clinical_unfunded: float
    reason: str
    caveat: str
