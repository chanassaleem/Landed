"""The group report: every module composed into one payload.

Pure composition. No I/O, no HTTP, no database. The web shell and the CLI both
call `build`, which is why the dashboard and the terminal can never disagree
about a number.

Two things happen here that no single module can do alone.

Per-location policy. Modules 1 and 3 are run once per (entity, location) using
that location's own demand history, not once over the group. The same item is
genuinely smooth in Karachi and intermittent in Lahore, and averaging the two
produces a safety stock that is wrong in both places. Module 4 then matches
surplus to need across those blocks.

A single ranked action list. Each module ranks its own output, but a manager
with forty minutes needs one list, in money order, across all of them: a
stockout, a transfer that avoids an import, a hospital that owes more than
anyone realised, and an instrument that will never repay its capital are
competing for the same attention. Ranking them against each other is the
product.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from engine import __version__, sanity
from engine.fmt import money
from engine.modules.cash import evaluate as evaluate_cash
from engine.modules.classify import classify
from engine.modules.forecast import evaluate as evaluate_forecast
from engine.modules.inventory import evaluate as evaluate_inventory
from engine.modules.machines import evaluate as evaluate_machines
from engine.modules.receivables import evaluate as evaluate_receivables
from engine.modules.redeploy import LocationBlock, suggest as suggest_transfers
from engine.modules.finance import evaluate as evaluate_finance
from engine.modules.marketing import evaluate as evaluate_marketing
from engine.payroll import Statutory, evaluate as evaluate_payroll
from engine.scope import Scope
from engine.modules.sequence import evaluate as evaluate_sequence
from engine.modules.service import evaluate as evaluate_service
from engine.schema import (
    Commitment,
    DemandPeriod,
    ItemSnapshot,
    MachinePlacement,
    OrderVerdict,
    PaybackStatus,
    ReceivableInvoice,
    ServiceJob,
    StockStatus,
    Thresholds,
    TransferUrgency,
)

# Severity drives both the ranking and the status lamp colour.
ALARM, CAUTION, WATCH, CLEAR = "alarm", "caution", "watch", "clear"
_SEVERITY_ORDER = {ALARM: 0, CAUTION: 1, WATCH: 2, CLEAR: 3}

MAX_INSIGHTS = 18


def plural(n: int, one: str) -> str:
    return f"{n} {one}" if n == 1 else f"{n} {one}s"


# --------------------------------------------------------------- blocks


def build_blocks(
    items: Sequence[ItemSnapshot],
    demand: Sequence[DemandPeriod],
    thresholds: Thresholds,
    as_of: date,
) -> List[LocationBlock]:
    """One block per (entity, location), each with its own demand history."""
    grouped: Dict[Tuple[str, str], Tuple[List[ItemSnapshot], List[DemandPeriod]]] = (
        defaultdict(lambda: ([], []))
    )
    for item in items:
        grouped[(item.entity_id, item.location_id)][0].append(item)
    for period in demand:
        grouped[(period.entity_id, period.location_id)][1].append(period)

    blocks: List[LocationBlock] = []
    for (entity, location), (its, hist) in sorted(grouped.items()):
        if not its:
            continue
        classes = classify(its, hist, thresholds)
        results = evaluate_inventory(its, classes, thresholds, as_of=as_of)
        blocks.append(
            LocationBlock(
                entity_id=entity,
                location_id=location,
                items=tuple(its),
                classes=classes,
                results=tuple(results),
            )
        )
    return blocks


# ------------------------------------------------------------- row shapes


def _stock_rows(blocks: Sequence[LocationBlock]) -> List[dict]:
    rows: List[dict] = []
    for block in blocks:
        source = {i.item_key: i for i in block.items}
        for r in block.results:
            item = source.get(r.item_key)
            cost = item.unit_cost if item else 0.0
            cls = block.classes.get(r.item_key)
            rows.append(
                {
                    "entity_id": block.entity_id,
                    "location_id": block.location_id,
                    "item_key": r.item_key,
                    "description": r.description,
                    "category": item.category if item else "",
                    # Carried so module 9 never has to re-derive them. A
                    # second derivation of the same figure is a second place
                    # for it to be wrong.
                    "lead_time_days": item.lead_time_days if item else 0.0,
                    "lead_time_days_std": item.lead_time_days_std if item else 0.0,
                    "is_clinical_critical": bool(
                        item.is_clinical_critical if item else False
                    ),
                    "mean_monthly_demand": round(cls.mean_monthly_demand, 2)
                    if cls else 0.0,
                    "annual_consumption_value": round(
                        cls.annual_consumption_value, 2
                    ) if cls else 0.0,
                    "demand_class": r.demand_class.value,
                    "abc": r.abc_class.value,
                    "service_target": r.service_target,
                    "service_metric": r.service_metric.value,
                    "on_hand": r.on_hand,
                    "open_po": r.open_po,
                    "position": r.position,
                    "safety_stock": round(r.safety_stock, 1),
                    "reorder_point": round(r.reorder_point, 1),
                    "order_up_to": round(r.order_up_to, 1),
                    "suggested_order_qty": r.suggested_order_qty,
                    "order_value": r.suggested_order_qty * cost,
                    "months_cover": round(r.months_cover, 2)
                    if r.months_cover is not None
                    else None,
                    "status": r.status.value,
                    "unit_cost": cost,
                    "stock_value": r.stock_value,
                    "excess_value": r.excess_value,
                    "days_to_expiry": r.days_to_expiry,
                    "reason": r.reason,
                }
            )
    return rows


def _transfer_rows(transfers) -> List[dict]:
    return [
        {
            "item_key": t.item_key,
            "description": t.description,
            "from_entity": t.from_entity,
            "from_location": t.from_location,
            "to_entity": t.to_entity,
            "to_location": t.to_location,
            "quantity": t.quantity,
            "unit_cost": t.unit_cost,
            "value_moved": t.value_moved,
            "import_avoided": t.import_avoided,
            "receiving_status": t.receiving_status,
            "sending_cover_after": t.sending_cover_after,
            "is_intercompany": t.is_intercompany,
            "urgency": t.urgency.value,
            "reason": t.reason,
        }
        for t in transfers
    ]


def _customer_rows(result) -> List[dict]:
    return [
        {
            "customer_key": c.customer_key,
            "customer_name": c.customer_name,
            "is_public_sector": c.is_public_sector,
            "entities": list(c.entities),
            "entity_count": len(c.entities),
            "invoice_count": c.invoice_count,
            "outstanding": c.outstanding,
            "overdue": c.overdue,
            "worst_days_overdue": c.worst_days_overdue,
            "weighted_avg_days_overdue": c.weighted_avg_days_overdue,
            "buckets": c.buckets,
            "financing_cost_annual": c.financing_cost_annual,
            "share_of_book": c.share_of_book,
            "reason": c.reason,
        }
        for c in result.customers
    ]


def _machine_rows(results) -> List[dict]:
    return [
        {
            "machine_key": m.machine_key,
            "model": m.model,
            "customer_name": m.customer_name,
            "entity_id": m.entity_id,
            "location_id": m.location_id,
            "months_elapsed": m.months_elapsed,
            "contract_months": m.contract_months,
            "months_remaining_on_contract": m.months_remaining_on_contract,
            "capital_cost": m.capital_cost,
            "margin_recovered": m.margin_recovered,
            "service_cost_to_date": m.service_cost_to_date,
            "net_recovered": m.net_recovered,
            "recovery_pct": m.recovery_pct,
            "monthly_margin_rate": m.monthly_margin_rate,
            "payback_months": m.payback_months,
            "utilisation": m.utilisation,
            "downtime_days_ytd": m.downtime_days_ytd,
            "revenue_lost_to_downtime": m.revenue_lost_to_downtime,
            "status": m.status.value,
            "unrecoverable_shortfall": m.unrecoverable_shortfall,
            "reason": m.reason,
        }
        for m in results
    ]


def _forecast_rows(results) -> List[dict]:
    return [
        {
            "item_key": f.item_key,
            "entity_id": f.entity_id,
            "location_id": f.location_id,
            "demand_class": f.demand_class.value,
            "method": f.method,
            "periods_observed": f.periods_observed,
            "mean_monthly_demand": f.mean_monthly_demand,
            "forecast": list(f.forecast),
            "lower": list(f.lower),
            "upper": list(f.upper),
            "next_month": f.forecast[0] if f.forecast else None,
            "next_month_upper": f.upper[0] if f.upper else None,
            "level": f.level,
            "trend": f.trend,
            "seasonality_fitted": f.seasonality_fitted,
            "mape": f.mape,
            "bias": f.bias,
            "reason": f.reason,
            "caveat": f.caveat,
        }
        for f in results
    ]


def _cash_rows(result) -> List[dict]:
    if result is None:
        return []
    return [
        {
            "index": w.index,
            "week": w.index + 1,
            "starts": w.starts.isoformat(),
            "ends": w.ends.isoformat(),
            "opening": w.opening,
            "receipts_receivables": w.receipts_receivables,
            "receipts_reagent": w.receipts_reagent,
            "receipts": round(w.receipts_receivables + w.receipts_reagent, 2),
            "payments_commitments": w.payments_commitments,
            "payments_orders": w.payments_orders,
            "payments_service": w.payments_service,
            "payments": round(
                w.payments_commitments + w.payments_orders + w.payments_service, 2
            ),
            "net": round(
                w.receipts_receivables + w.receipts_reagent
                - w.payments_commitments - w.payments_orders - w.payments_service, 2
            ),
            "closing": w.closing,
            "is_negative": w.is_negative,
            "lines": list(w.lines),
        }
        for w in result.weeks
    ]


def _order_rows(result) -> List[dict]:
    if result is None:
        return []
    return [
        {
            "item_key": o.item_key,
            "description": o.description,
            "entity_id": o.entity_id,
            "location_id": o.location_id,
            "quantity": o.quantity,
            "value": o.value,
            "verdict": o.verdict.value,
            "week_index": o.week_index,
            "week": (o.week_index + 1) if o.week_index is not None else None,
            "place_by": o.place_by.isoformat() if o.place_by else None,
            "arrives": o.arrives.isoformat() if o.arrives else None,
            "days_of_cover_left": o.days_of_cover_left,
            "is_clinical_critical": o.is_clinical_critical,
            "abc": o.abc_class.value,
            "priority_score": o.priority_score,
            "stockout_cost_per_rupee": o.stockout_cost_per_rupee,
            "covered_by_transfer": o.covered_by_transfer,
            "reason": o.reason,
        }
        for o in result.orders
    ]


# ------------------------------------------------------------- insights


def _insight(severity, domain, title, detail, value, action, ref=""):
    return {
        "severity": severity,
        "domain": domain,
        "title": title,
        "detail": detail,
        "value": round(value, 2),
        "action": action,
        "ref": ref,
    }


def _rank_insights(
    stock: List[dict],
    transfers: List[dict],
    ar,
    svc,
    machines: List[dict],
    cash,
    sequencing,
    thresholds: Thresholds,
    forecast_rows: Optional[List[dict]] = None,
    findings: Sequence = (),
) -> List[dict]:
    """One list, in money order, across every module."""
    out: List[dict] = []

    # A blocking data fault comes first whatever the money at stake, because
    # it means a figure on this page is wrong rather than merely unwelcome.
    # Acting on a confidently wrong number is worse than acting on none.
    blocking = [f for f in findings if f.severity == sanity.BLOCKS]
    if blocking:
        out.append(
            _insight(
                ALARM, "data",
                f"{plural(len(blocking), 'data fault')} "
                f"{'makes' if len(blocking) == 1 else 'make'} some figures on "
                f"this page wrong",
                "; ".join(f.title for f in blocking[:3])
                + ("; and others" if len(blocking) > 3 else "")
                + ". Each one is explained on the Data sanity screen, with the "
                  "rows named and what it does to the numbers.",
                0.0,
                "Fix these before acting on anything else here.",
            )
        )

    # --- stock that runs out
    for r in stock:
        if r["status"] == StockStatus.CRITICAL.value:
            out.append(
                _insight(
                    ALARM, "stock",
                    f'{r["item_key"]} runs out at {r["location_id"]}',
                    r["reason"],
                    r["order_value"],
                    "Order now, or transfer from another branch.",
                    r["item_key"],
                )
            )

    # --- transfers, which release cash rather than commit it
    urgent = [t for t in transfers if t["urgency"] == TransferUrgency.URGENT.value]
    if urgent:
        total = sum(t["import_avoided"] for t in urgent)
        out.append(
            _insight(
                CAUTION, "transfers",
                f"{len(urgent)} transfer{'s' if len(urgent) != 1 else ''} would "
                f"cover a stockout without importing",
                "Another branch already holds this stock above its own policy "
                "level. A transfer lands this week; an import lands in two to "
                "three months behind a letter of credit.",
                total,
                "Approve the transfers on the Transfers screen.",
            )
        )
    other = [t for t in transfers if t["urgency"] != TransferUrgency.URGENT.value]
    if other:
        out.append(
            _insight(
                WATCH, "transfers",
                f"{len(other)} further transfer{'s' if len(other) != 1 else ''} "
                f"would avoid ordering",
                "Surplus at one branch against a reorder point at another.",
                sum(t["import_avoided"] for t in other),
                "Review before the next purchasing run.",
            )
        )

    # --- expiry, which is a write-off rather than a delay
    expiring = [r for r in stock if r["status"] == StockStatus.EXPIRY_RISK.value]
    if expiring:
        out.append(
            _insight(
                CAUTION, "stock",
                f"{len(expiring)} item{'s' if len(expiring) != 1 else ''} "
                f"will expire before {'they are' if len(expiring) != 1 else 'it is'} used",
                "Cover looks comfortable, but the stock dies before the demand "
                "arrives. This is a write-off, not a delay.",
                sum(r["excess_value"] for r in expiring),
                "Move it to a branch that will consume it, or discount it now.",
            )
        )

    # --- cash trapped in excess
    excess_value = sum(
        r["excess_value"] for r in stock if r["status"] == StockStatus.EXCESS.value
    )
    if excess_value > 0:
        out.append(
            _insight(
                WATCH, "stock",
                "Cash sitting in stock above policy",
                f"Held above the {thresholds.excess_cover_months:.0f} month cover "
                f"limit, financed at {thresholds.cost_of_capital_annual:.0%} a year.",
                excess_value,
                "Stop reordering these lines until cover is back inside policy.",
            )
        )

    # --- the order book against the cash to fund it
    order_value = sum(r["order_value"] for r in stock)
    stock_value = sum(r["stock_value"] for r in stock)

    if sequencing is not None and sequencing.value_unfunded > 0:
        # Cash is known, so the answer is specific rather than a warning.
        out.append(
            _insight(
                ALARM, "purchasing",
                f"{money(sequencing.value_unfunded)} of required orders cannot "
                f"be funded in {sequencing.horizon_weeks} weeks",
                sequencing.reason,
                sequencing.value_unfunded,
                "Work the refused list: finance it, split the shipment, or "
                "accept the stockout deliberately rather than by default.",
            )
        )
        if sequencing.clinical_unfunded > 0:
            out.append(
                _insight(
                    ALARM, "purchasing",
                    "Clinically critical stock is among what cannot be funded",
                    f"{money(sequencing.clinical_unfunded)} of the unfunded book "
                    f"is flagged clinically critical. A stockout here stops a "
                    f"hospital testing, so the decision belongs to someone who "
                    f"can authorise financing.",
                    sequencing.clinical_unfunded,
                    "Escalate today. This is not a purchasing decision.",
                )
            )
        if sequencing.value_placed_now > 0:
            out.append(
                _insight(
                    WATCH, "purchasing",
                    f"{money(sequencing.value_placed_now)} of orders are funded "
                    f"and ready to place",
                    "Ranked by what a stockout costs per rupee committed, with "
                    "clinical items first and anything a transfer already "
                    "covers taken out.",
                    sequencing.value_placed_now,
                    "Release these purchase orders.",
                )
            )
    elif order_value > stock_value and stock_value > 0:
        out.append(
            _insight(
                ALARM, "cash",
                "Policy is asking for more stock than you hold",
                f"Suggested orders of {money(order_value)} against "
                f"{money(stock_value)} of stock on hand. Enter an opening bank "
                f"balance and the engine will say which of these can actually "
                f"be placed.",
                order_value - stock_value,
                "Add the bank balance in settings to get a funded schedule.",
            )
        )

    # --- cash weeks that break the floor
    if cash is not None and cash.weeks_negative:
        out.append(
            _insight(
                ALARM, "cash",
                f"Cash drops below the floor in "
                f"{plural(cash.weeks_negative, 'week')}",
                cash.reason,
                max(0.0, thresholds.cash_buffer - cash.lowest_balance),
                "Pull collections forward or move a commitment before week "
                f"{cash.lowest_week_index + 1}.",
            )
        )

    # --- forecasts nobody should trust yet
    unmeasured = [f for f in (forecast_rows or []) if f["mape"] is None]
    if unmeasured:
        out.append(
            _insight(
                WATCH, "forecast",
                f"{plural(len(unmeasured), 'item')} have too little history to "
                f"measure forecast accuracy",
                "Policy for these rests on a forecast whose error has never "
                "been tested. It is not wrong, it is unverified, and the "
                "difference matters when it sets a reorder point.",
                sum(r["order_value"] for r in stock
                    if any(f["item_key"] == r["item_key"] for f in unmeasured)),
                "Export more demand history, ideally 24 months.",
            )
        )

    # --- receivables
    if ar is not None:
        for c in ar.customers[:5]:
            if c.worst_days_overdue >= 180:
                out.append(
                    _insight(
                        ALARM, "receivables",
                        f"{c.customer_name} is past 180 days",
                        c.reason,
                        c.overdue,
                        "Escalate before committing more stock to this customer.",
                        c.customer_key,
                    )
                )
            elif len(c.entities) > 1 and c.overdue > 0:
                out.append(
                    _insight(
                        CAUTION, "receivables",
                        f"{c.customer_name} owes money in {len(c.entities)} entities",
                        c.reason,
                        c.overdue,
                        "Set one group credit limit for this customer.",
                        c.customer_key,
                    )
                )
        if ar.financing_cost_annual > 0:
            out.append(
                _insight(
                    WATCH, "receivables",
                    "Annual cost of carrying the overdue book",
                    f"{money(ar.overdue)} overdue at "
                    f"{thresholds.cost_of_capital_annual:.0%}. This is an interest "
                    f"cost, not a collections statistic.",
                    ar.financing_cost_annual,
                    "Collect the oldest public-sector balances first.",
                )
            )

    # --- service wastage
    if svc is not None and svc.total_value_lost > 0:
        out.append(
            _insight(
                CAUTION, "service",
                "Service work done and never billed",
                svc.reason,
                svc.total_value_lost,
                "Require hours and parts on the job sheet before it is closed.",
            )
        )

    # --- placements that will not repay their capital
    for m in machines:
        if m["status"] == PaybackStatus.UNRECOVERABLE.value:
            out.append(
                _insight(
                    ALARM, "equipment",
                    f'{m["machine_key"]} will not repay its capital',
                    m["reason"],
                    m["unrecoverable_shortfall"],
                    "Renegotiate the volume commitment or recover the instrument.",
                    m["machine_key"],
                )
            )
        elif m["status"] == PaybackStatus.STALLED.value:
            out.append(
                _insight(
                    CAUTION, "equipment",
                    f'{m["machine_key"]} has stalled',
                    m["reason"],
                    max(0.0, m["capital_cost"] - m["net_recovered"]),
                    "Find out whether it is installed, trained and running.",
                    m["machine_key"],
                )
            )

    downtime = sum(m["revenue_lost_to_downtime"] for m in machines)
    if downtime > 0:
        out.append(
            _insight(
                WATCH, "equipment",
                "Reagent margin lost while instruments were down",
                "Downtime costs more in lost consumable pull than in engineer "
                "hours. The two are the same problem seen twice.",
                downtime,
                "Prioritise uptime on the highest-pull placements.",
            )
        )

    # Value descending within a severity, except the data fault, which is
    # pinned to the top: its value is zero by nature and it still outranks
    # everything, because every other figure depends on it.
    out.sort(key=lambda i: (
        0 if i["domain"] == "data" else 1,
        _SEVERITY_ORDER[i["severity"]],
        -i["value"],
    ))
    return out[:MAX_INSIGHTS]


# ----------------------------------------------------------------- build


def public(payload: dict) -> dict:
    """The payload without its private keys, ready to serialise.

    `build` returns a few underscore-prefixed keys holding module objects that
    the persistence layer needs and JSON cannot represent. Any caller sending
    the payload anywhere should go through here rather than remembering which
    keys to drop.
    """
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def _statutory(t: Thresholds) -> Statutory:
    """Payroll's statutory rates, from the one settings object.

    They live on Thresholds so the person changes them in the same place as
    every other assumption, and payroll keeps its own dataclass so the rates
    can be passed around without dragging the whole settings object into a
    tax calculation.
    """
    return Statutory(
        minimum_wage_monthly=t.minimum_wage_monthly,
        social_security_wage_ceiling=t.social_security_wage_ceiling,
    )


def build(
    items: Sequence[ItemSnapshot] = (),
    demand: Sequence[DemandPeriod] = (),
    invoices: Sequence[ReceivableInvoice] = (),
    jobs: Sequence[ServiceJob] = (),
    placements: Sequence[MachinePlacement] = (),
    commitments: Sequence[Commitment] = (),
    accounts: Sequence = (),
    employees: Sequence = (),
    opportunities: Sequence = (),
    marketing_spend: Sequence = (),
    thresholds: Thresholds | None = None,
    as_of: Optional[date] = None,
    monthly_revenue: Optional[float] = None,
    opening_balance: Optional[float] = None,
    collection_rate: float = 1.0,
    scope: Scope | None = None,
    group: dict | None = None,
    scope_counts: dict | None = None,
) -> dict:
    """Run every module that has data and return one JSON-safe payload.

    The facts passed in are already narrowed to `scope`; `group` carries the
    unnarrowed ones for the two modules that have to see the whole group to
    mean anything. See engine/scope.py for why those two are different.
    """
    thresholds = thresholds or Thresholds()
    as_of = as_of or date.today()
    scope = scope or Scope()
    # Without the group's facts the two group-level modules fall back to the
    # scoped ones, which is right when nothing is scoped and is the only
    # sensible reading when a caller supplies no group set.
    group = group or {}
    g_items = group.get("items", items)
    g_demand = group.get("demand", demand)
    g_invoices = group.get("invoices", invoices)
    g_jobs = group.get("jobs", jobs)
    g_placements = group.get("placements", placements)
    g_commitments = group.get("commitments", commitments)

    blocks = build_blocks(items, demand, thresholds, as_of)
    stock = _stock_rows(blocks)

    # Transfers need both ends of the move, so they are computed across the
    # group and then narrowed to the moves this branch is party to -- in or
    # out, because a branch cares about stock arriving as much as leaving.
    group_blocks = (blocks if not scope.active
                    else build_blocks(g_items, g_demand, thresholds, as_of))
    all_transfers = _transfer_rows(suggest_transfers(group_blocks, thresholds))
    transfers = [
        t for t in all_transfers
        if not scope.active
        or (scope.entity in (None, t["from_entity"], t["to_entity"])
            and scope.location in (None, t["from_location"], t["to_location"]))
    ]

    ar = evaluate_receivables(invoices, thresholds, as_of, monthly_revenue) if invoices else None
    svc = evaluate_service(jobs, thresholds, as_of) if jobs else None
    mx = evaluate_machines(placements, thresholds, as_of) if placements else ()
    machines = _machine_rows(mx)

    # Module 2. One forecast per item per location, using that location's own
    # history, which is why it runs per block rather than over the group.
    fx = []
    for block in blocks:
        fx.extend(
            evaluate_forecast(
                block.items,
                [d for d in demand
                 if d.entity_id == block.entity_id
                 and d.location_id == block.location_id],
                block.classes,
                thresholds,
                as_of,
            )
        )
    forecasts = _forecast_rows(fx)

    # Modules 8 and 9 in order, never the other way. Module 8 deliberately
    # excludes new orders so that its headroom is what module 9 packs into;
    # computing them together would be circular.
    cash = (
        evaluate_cash(
            invoices=g_invoices,
            commitments=g_commitments,
            placements=g_placements,
            jobs=g_jobs,
            thresholds=thresholds,
            as_of=as_of,
            opening_balance=opening_balance,
            collection_rate=collection_rate,
        )
        if (invoices or commitments or placements or jobs)
        else None
    )
    sequencing = (
        evaluate_sequence(stock, cash, transfers, thresholds, as_of)
        if stock
        else None
    )
    cash_weeks = _cash_rows(cash)
    orders = _order_rows(sequencing)

    # Modules 10 and 11. Finance is group-level by nature -- a trial balance
    # belongs to a company, not to a branch -- so it runs on whatever was
    # supplied without further narrowing. The pipeline is checked against the
    # stock position and the cash headroom this run computed, which is the
    # whole reason it lives here rather than in a CRM.
    statements = evaluate_finance(
        accounts, thresholds,
        period_from=date(as_of.year, 1, 1), period_to=as_of,
    ) if accounts else None

    staff = evaluate_payroll(employees, _statutory(thresholds), as_of) \
        if employees else None

    pipeline = evaluate_marketing(
        opportunities, marketing_spend, stock,
        cash_headroom=(cash.headroom_for_orders if cash else None),
        thresholds=thresholds, as_of=as_of,
    ) if opportunities else None

    # Data sanity. Runs over the records rather than the results, because the
    # point is what the inputs do to the outputs. Reports only: it never
    # changes a figure.
    findings = sanity.check(
        items=items, demand=demand, invoices=invoices, jobs=jobs,
        placements=placements, commitments=commitments, as_of=as_of,
    )
    sanity_summary = sanity.summary(findings)

    counts = {s.value: 0 for s in StockStatus}
    for r in stock:
        counts[r["status"]] += 1

    stock_value = sum(r["stock_value"] for r in stock)
    order_value = sum(r["order_value"] for r in stock)
    trapped = sum(r["excess_value"] for r in stock)
    capital = sum(m["capital_cost"] for m in machines)
    recovered = sum(m["net_recovered"] for m in machines)

    group = {
        # stock
        "stock_value": round(stock_value, 2),
        "cash_trapped": round(trapped, 2),
        "suggested_po_value": round(order_value, 2),
        "unfundable": order_value > stock_value and stock_value > 0,
        "funding_gap": round(max(0.0, order_value - stock_value), 2),
        "items": len(stock),
        "locations": len(blocks),
        "entities": len({b.entity_id for b in blocks}),
        # transfers
        "transfer_count": len(transfers),
        "imports_avoided": round(sum(t["import_avoided"] for t in transfers), 2),
        # money
        "outstanding": round(ar.outstanding, 2) if ar else None,
        "overdue": round(ar.overdue, 2) if ar else None,
        "intercompany_excluded": round(ar.intercompany_excluded, 2) if ar else None,
        "dso_days": ar.dso_days if ar else None,
        "dso_gap_days": ar.dso_gap_days if ar else None,
        "cash_released_at_target": round(ar.cash_released_at_target, 2) if ar else None,
        "receivables_financing_cost": round(ar.financing_cost_annual, 2) if ar else None,
        "public_sector_share": ar.public_sector_share if ar else None,
        # service
        "service_value_lost": round(svc.total_value_lost, 2) if svc else None,
        "service_recovery_rate": svc.recovery_rate if svc else None,
        "repeat_visit_rate": svc.repeat_visit_rate if svc else None,
        "downtime_days": svc.downtime_days if svc else None,
        # equipment
        "capital_deployed": round(capital, 2) if machines else None,
        "capital_recovered": round(recovered, 2) if machines else None,
        "capital_recovery_pct": round(recovered / capital, 4)
        if capital > 0
        else None,
        "downtime_margin_lost": round(
            sum(m["revenue_lost_to_downtime"] for m in machines), 2
        )
        if machines
        else None,
        "placements": len(machines),
        # cash
        "opening_balance": opening_balance,
        "cash_lowest": cash.lowest_balance if cash else None,
        "cash_lowest_week": (cash.lowest_week_index + 1) if cash else None,
        "cash_weeks_negative": cash.weeks_negative if cash else None,
        "cash_headroom": cash.headroom_for_orders if cash else None,
        "cash_receipts": cash.total_receipts if cash else None,
        "cash_payments": cash.total_payments if cash else None,
        "cash_closing": cash.closing_balance if cash else None,
        # sequencing
        "orders_requested": sequencing.value_requested if sequencing else None,
        "orders_place_now": sequencing.value_placed_now if sequencing else None,
        "orders_scheduled": sequencing.value_scheduled if sequencing else None,
        "orders_unfunded": sequencing.value_unfunded if sequencing else None,
        "orders_by_transfer": sequencing.value_covered_by_transfer if sequencing else None,
        "orders_clinical_unfunded": sequencing.clinical_unfunded if sequencing else None,
        # forecast
        "forecast_items": len(forecasts),
        "forecast_measured": len([f for f in forecasts if f["mape"] is not None]),
        "forecast_seasonal": len([f for f in forecasts if f["seasonality_fitted"]]),
    }

    # The headline: one sentence, stated the way an analyser states a result.
    if not stock:
        headline = "No stock data loaded."
    elif group["unfundable"]:
        headline = (
            f"Stock stands at {money(stock_value)}. Policy calls for "
            f"{money(order_value)} of orders, which is "
            f"{money(group['funding_gap'])} more than the stock you hold."
        )
    else:
        headline = (
            f"Stock stands at {money(stock_value)} with "
            f"{money(trapped)} held above policy. Orders of "
            f"{money(order_value)} are within what you hold."
        )
    if transfers:
        headline += (
            f" {money(group['imports_avoided'])} of that could come from "
            f"another branch instead of an import."
        )

    # Once cash is known the question stops being what policy wants and
    # becomes what can actually be placed, which is the only version anyone
    # can act on.
    if sequencing and cash and opening_balance is not None:
        headline = (
            f"Policy asks for {money(sequencing.value_requested)} of orders. "
            f"{money(sequencing.value_placed_now)} can be placed this week, "
            f"{money(sequencing.value_scheduled)} fits later, and "
            f"{money(sequencing.value_unfunded)} does not fit at all."
        )
        if sequencing.value_covered_by_transfer > 0:
            headline += (
                f" {money(sequencing.value_covered_by_transfer)} comes off the "
                f"book because a transfer already covers it."
            )

    # Raw module output for the persistence layer. The web shell pops these
    # before responding; they are objects, not JSON.
    raw_results = [r for b in blocks for r in b.results]
    raw_order_values = {
        r["item_key"]: r["order_value"] for r in stock if r["order_value"]
    }

    return {
        "_results": raw_results,
        "_order_values": raw_order_values,
        "as_of": as_of.isoformat(),
        "engine_version": __version__,
        "headline": headline,
        "group": group,
        # The selector's own options come from the group's blocks, never the
        # scoped ones: narrowing the list to what is already selected leaves
        # no way back to the rest of the business.
        "scope": {
            "entities": sorted({b.entity_id for b in group_blocks}),
            "locations": [
                {"entity_id": b.entity_id, "location_id": b.location_id,
                 "items": len(b.results)}
                for b in group_blocks
            ],
            "entity": scope.entity,
            "location": scope.location,
            "active": scope.active,
            "label": scope.label(),
            # Named here rather than left for each screen to remember, so a
            # figure computed on the whole group can never be shown under a
            # branch heading without saying so.
            "group_level": (
                ["cash", "transfers"]
                + (["finance"] if scope.location else [])
            ) if scope.active else [],
            "kept": scope_counts or {},
        },
        "stock": {"rows": stock, "counts": counts},
        "transfers": {"rows": transfers},
        "receivables": {
            "buckets": ar.buckets if ar else {},
            "customers": _customer_rows(ar) if ar else [],
            "dso_days": ar.dso_days if ar else None,
            "target_dso_days": thresholds.target_dso_days,
            "outstanding": ar.outstanding if ar else 0.0,
            "overdue": ar.overdue if ar else 0.0,
            "intercompany_excluded": ar.intercompany_excluded if ar else 0.0,
            "financing_cost_annual": ar.financing_cost_annual if ar else 0.0,
            "cash_released_at_target": ar.cash_released_at_target if ar else 0.0,
            "public_sector_share": ar.public_sector_share if ar else None,
        },
        "service": {
            "jobs": svc.jobs if svc else 0,
            "jobs_open": svc.jobs_open if svc else 0,
            "labour_hours": svc.labour_hours if svc else 0.0,
            "labour_hours_unbilled": svc.labour_hours_unbilled if svc else 0.0,
            "labour_value_lost": svc.labour_value_lost if svc else 0.0,
            "parts_cost": svc.parts_cost if svc else 0.0,
            "parts_uncharged": svc.parts_uncharged if svc else 0.0,
            "total_value_lost": svc.total_value_lost if svc else 0.0,
            "recovery_rate": svc.recovery_rate if svc else None,
            "repeat_visit_rate": svc.repeat_visit_rate if svc else None,
            "mean_days_to_close": svc.mean_days_to_close if svc else None,
            "downtime_days": svc.downtime_days if svc else 0.0,
            "by_machine": [dict(m) for m in (svc.by_machine if svc else ())],
            "reason": svc.reason if svc else "",
        },
        "machines": {"rows": machines},
        "forecast": {
            "rows": forecasts,
            "horizon_months": thresholds.forecast_horizon_months,
            "min_periods_for_seasonality": thresholds.min_periods_for_seasonality,
        },
        # Modules 10 and 11.
        "finance": {
            "has": statements is not None,
            "period_from": statements.period_from.isoformat() if statements else None,
            "period_to": statements.period_to.isoformat() if statements else None,
            "pnl": {
                "revenue": statements.revenue if statements else 0.0,
                "cogs": statements.cogs if statements else 0.0,
                "gross_profit": statements.gross_profit if statements else 0.0,
                "gross_margin": statements.gross_margin if statements else 0.0,
                "payroll": statements.payroll if statements else 0.0,
                "opex": statements.opex if statements else 0.0,
                "depreciation": statements.depreciation if statements else 0.0,
                "operating_profit": statements.operating_profit if statements else 0.0,
                "finance_cost": statements.finance_cost if statements else 0.0,
                "other_income": statements.other_income if statements else 0.0,
                "profit_before_tax": statements.profit_before_tax if statements else 0.0,
                "tax": statements.tax if statements else 0.0,
                "profit_after_tax": statements.profit_after_tax if statements else 0.0,
                "net_margin": statements.net_margin if statements else 0.0,
                "rows": list(statements.pnl_rows) if statements else [],
            },
            "balance_sheet": {
                "assets_opening": statements.assets_opening if statements else 0.0,
                "assets_closing": statements.assets_closing if statements else 0.0,
                "liabilities_closing": statements.liabilities_closing if statements else 0.0,
                "equity_closing": statements.equity_closing if statements else 0.0,
                "cash_opening": statements.cash_opening if statements else 0.0,
                "cash_closing": statements.cash_closing if statements else 0.0,
                "working_capital": statements.working_capital if statements else 0.0,
                "current_ratio": statements.current_ratio if statements else None,
                "gearing": statements.gearing if statements else None,
                "rows": list(statements.bs_rows) if statements else [],
            },
            "cash_flow": {
                "operating": statements.cash_from_operations if statements else 0.0,
                "investing": statements.cash_from_investing if statements else 0.0,
                "financing": statements.cash_from_financing if statements else 0.0,
                "net_movement": statements.net_cash_movement if statements else 0.0,
                "operating_lines": [
                    {"label": n, "amount": v}
                    for n, v in (statements.operating_lines if statements else ())],
                "investing_lines": [
                    {"label": n, "amount": v}
                    for n, v in (statements.investing_lines if statements else ())],
                "financing_lines": [
                    {"label": n, "amount": v}
                    for n, v in (statements.financing_lines if statements else ())],
            },
            "checks": [
                {"name": c.name, "expected": c.expected, "actual": c.actual,
                 "difference": c.difference, "passed": c.passed,
                 "meaning": c.meaning}
                for c in (statements.checks if statements else ())
            ],
            "ties": statements.ties if statements else None,
            "reason": statements.reason if statements else "",
            "caveat": statements.caveat if statements else "",
        },
        "payroll": {
            "has": staff is not None,
            "rows": [
                {
                    "employee_id": s_.employee_id, "name": s_.name,
                    "entity_id": s_.entity_id, "location_id": s_.location_id,
                    "department": s_.department,
                    "gross_monthly": s_.gross_monthly,
                    "taxable_annual": s_.taxable_annual,
                    "tax_monthly": s_.tax_monthly,
                    "eobi_employee": s_.eobi_employee,
                    "pf_employee": s_.pf_employee,
                    "net_monthly": s_.net_monthly,
                    "eobi_employer": s_.eobi_employer,
                    "social_security_employer": s_.social_security_employer,
                    "pf_employer": s_.pf_employer,
                    "gratuity_accrual": s_.gratuity_accrual,
                    "employer_cost": s_.employer_cost,
                    "effective_tax_rate": s_.effective_tax_rate,
                    "reason": s_.reason,
                }
                for s_ in (staff.slips if staff else ())
            ],
            "headcount": staff.headcount if staff else 0,
            "gross_monthly": staff.gross_monthly if staff else 0.0,
            "tax_monthly": staff.tax_monthly if staff else 0.0,
            "eobi_total": staff.eobi_total if staff else 0.0,
            "social_security_total": staff.social_security_total if staff else 0.0,
            "pf_total": staff.pf_total if staff else 0.0,
            "gratuity_total": staff.gratuity_total if staff else 0.0,
            "net_monthly": staff.net_monthly if staff else 0.0,
            "employer_cost_monthly": staff.employer_cost_monthly if staff else 0.0,
            "employer_cost_annual": staff.employer_cost_annual if staff else 0.0,
            "by_department": staff.by_department if staff else {},
            "table_name": staff.table_name if staff else "",
            "table_source": staff.table_source if staff else "",
            "reason": staff.reason if staff else "",
            "caveat": staff.caveat if staff else "",
        },
        "marketing": {
            "has": pipeline is not None,
            "rows": [
                {
                    "opportunity_id": r_.opportunity_id,
                    "customer_key": r_.customer_key,
                    "customer_name": r_.customer_name,
                    "entity_id": r_.entity_id, "location_id": r_.location_id,
                    "stage": r_.stage, "value": r_.value, "odds": r_.odds,
                    "weighted": r_.weighted,
                    "odds_measured": r_.odds_measured,
                    "days_to_close": r_.days_to_close,
                    "expected_close": r_.expected_close,
                    "item_key": r_.item_key, "quantity": r_.quantity,
                    "supply": r_.supply, "source": r_.source,
                    "owner": r_.owner, "age_days": r_.age_days,
                    "reason": r_.reason,
                }
                for r_ in (pipeline.rows if pipeline else ())
            ],
            "open_count": pipeline.open_count if pipeline else 0,
            "open_value": pipeline.open_value if pipeline else 0.0,
            "weighted_value": pipeline.weighted_value if pipeline else 0.0,
            "won_value": pipeline.won_value if pipeline else 0.0,
            "lost_value": pipeline.lost_value if pipeline else 0.0,
            "win_rate": pipeline.win_rate if pipeline else None,
            "win_rate_measured": pipeline.win_rate_measured if pipeline else False,
            "spend": pipeline.spend if pipeline else 0.0,
            "cost_per_win": pipeline.cost_per_win if pipeline else None,
            "spend_per_rupee_won": pipeline.spend_per_rupee_won if pipeline else None,
            "by_stage": pipeline.by_stage if pipeline else {},
            "by_source": pipeline.by_source if pipeline else {},
            "at_risk_supply": list(pipeline.at_risk_supply) if pipeline else [],
            "unfundable": pipeline.unfundable if pipeline else 0.0,
            "reason": pipeline.reason if pipeline else "",
            "caveat": pipeline.caveat if pipeline else "",
        },
        "cash": {
            "weeks": cash_weeks,
            "opening_balance": opening_balance,
            "lowest_balance": cash.lowest_balance if cash else None,
            "lowest_week": (cash.lowest_week_index + 1) if cash else None,
            "weeks_negative": cash.weeks_negative if cash else None,
            "headroom": cash.headroom_for_orders if cash else None,
            "total_receipts": cash.total_receipts if cash else None,
            "total_payments": cash.total_payments if cash else None,
            "closing_balance": cash.closing_balance if cash else None,
            "buffer": thresholds.cash_buffer,
            "reason": cash.reason if cash else "",
            "caveat": cash.caveat if cash else "",
        },
        "purchasing": {
            "rows": orders,
            "horizon_weeks": sequencing.horizon_weeks if sequencing else 0,
            "requested": sequencing.value_requested if sequencing else 0.0,
            "place_now": sequencing.value_placed_now if sequencing else 0.0,
            "scheduled": sequencing.value_scheduled if sequencing else 0.0,
            "unfunded": sequencing.value_unfunded if sequencing else 0.0,
            "by_transfer": sequencing.value_covered_by_transfer if sequencing else 0.0,
            "clinical_unfunded": sequencing.clinical_unfunded if sequencing else 0.0,
            "reason": sequencing.reason if sequencing else "",
            "caveat": sequencing.caveat if sequencing else "",
        },
        "sanity": {
            "summary": sanity_summary,
            "findings": [
                {
                    "code": f.code,
                    "severity": f.severity,
                    "dataset": f.dataset,
                    "title": f.title,
                    "detail": f.detail,
                    "effect": f.effect,
                    "fix": f.fix,
                    "rows": list(f.rows),
                    "count": f.count,
                }
                for f in findings
            ],
        },
        "insights": _rank_insights(
            stock, transfers, ar, svc, machines, cash, sequencing, thresholds,
            forecasts, findings,
        ),
        "settings": {
            "target_cover_months": thresholds.target_cover_months,
            "excess_cover_months": thresholds.excess_cover_months,
            "cost_of_capital_annual": thresholds.cost_of_capital_annual,
            "target_dso_days": thresholds.target_dso_days,
            "labour_rate_per_hour": thresholds.labour_rate_per_hour,
            "payback_target_months": thresholds.payback_target_months,
            "min_transfer_value": thresholds.min_transfer_value,
            "sender_min_cover_months": thresholds.sender_min_cover_months,
            "placement_min_months_to_judge": thresholds.placement_min_months_to_judge,
            "clinical_critical_floor": thresholds.clinical_critical_floor,
            "monthly_revenue": monthly_revenue,
            "opening_balance": opening_balance,
            "collection_rate": collection_rate,
            "cash_buffer": thresholds.cash_buffer,
            "cash_horizon_weeks": thresholds.cash_horizon_weeks,
            "forecast_horizon_months": thresholds.forecast_horizon_months,
            "min_periods_for_seasonality": thresholds.min_periods_for_seasonality,
            "overdue_catchup_weeks": thresholds.overdue_catchup_weeks,
        },
        "loaded": {
            "inventory": len(items),
            "demand": len(demand),
            "receivables": len(invoices),
            "service": len(jobs),
            "machines": len(placements),
            "commitments": len(commitments),
        },
    }
