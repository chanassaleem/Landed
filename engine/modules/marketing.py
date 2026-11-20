"""Module 11. Pipeline, tenders and what they cost to win.

A distributor's marketing is not campaigns and impressions. It is a list of
tenders and accounts being worked, a spend on demonstrations, conferences and
sample reagents, and a question nobody answers until it is too late: if we
win this, can we supply it and can we fund it?

That last question is why this is here rather than in a CRM. The pipeline is
the only part of the business that is a claim about the future, and this
product already knows what the stock position and the cash position are. So
the module does three things:

  It sizes the pipeline honestly. Weighted by the stage's own historical win
  rate where there is one, not by a number somebody typed in the hope box.

  It prices the effort. Spend against wins gives a cost per win and a cost
  per rupee of revenue, which is the only marketing figure a small business
  owner can act on.

  It checks the pipeline against the warehouse and the bank. An opportunity
  closing in six weeks for an item with eleven weeks of lead time and no
  stock is not a forecast, it is a disappointment with a date on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

from engine.schema import Thresholds

STAGES = ("lead", "qualified", "quoted", "tender_submitted", "won", "lost")
OPEN_STAGES = ("lead", "qualified", "quoted", "tender_submitted")

# Where a stage has too little history to measure its own win rate, these
# are the fallbacks. They are assumptions, labelled as such wherever a figure
# that used them is shown.
DEFAULT_ODDS = {
    "lead": 0.10,
    "qualified": 0.25,
    "quoted": 0.40,
    "tender_submitted": 0.50,
}
MIN_FOR_MEASURED_ODDS = 8


@dataclass(frozen=True)
class Opportunity:
    opportunity_id: str
    customer_key: str
    customer_name: str
    entity_id: str
    location_id: str
    stage: str
    value: float
    expected_close: date | None = None
    item_key: str = ""
    quantity: float = 0.0
    source: str = ""            # tender, referral, conference, cold
    owner: str = ""
    opened: date | None = None
    closed: date | None = None


@dataclass(frozen=True)
class Spend:
    entity_id: str
    label: str
    category: str               # conference, demo_unit, samples, digital, print
    spent_on: date
    amount: float


@dataclass(frozen=True)
class PipelineRow:
    opportunity_id: str
    customer_key: str
    customer_name: str
    entity_id: str
    location_id: str
    stage: str
    value: float
    odds: float
    weighted: float
    odds_measured: bool
    days_to_close: int | None
    expected_close: str
    item_key: str
    quantity: float
    supply: str
    source: str
    owner: str
    age_days: int | None
    reason: str


@dataclass(frozen=True)
class MarketingResult:
    rows: tuple
    open_count: int
    open_value: float
    weighted_value: float
    won_value: float
    lost_value: float
    win_rate: float | None
    win_rate_measured: bool
    spend: float
    cost_per_win: float | None
    spend_per_rupee_won: float | None
    by_stage: dict
    by_source: dict
    at_risk_supply: tuple
    unfundable: float
    reason: str
    caveat: str


def measured_odds(opportunities: Sequence[Opportunity]) -> tuple[dict, dict]:
    """Win rate per stage, from what actually closed.

    A stage is only given its own rate once enough has gone through it to
    mean anything; below that the default is used and the figure is marked
    as an assumption.
    """
    odds, measured = {}, {}
    closed = [o for o in opportunities if o.stage in ("won", "lost")]
    for stage in OPEN_STAGES:
        # Everything that reached this stage and then closed. Without stage
        # history on each record this is an approximation, and it is the
        # honest one available: all closed deals from that source.
        seen = [o for o in closed]
        if len(seen) >= MIN_FOR_MEASURED_ODDS:
            won = sum(1 for o in seen if o.stage == "won")
            base = won / len(seen)
            # Later stages are proportionally likelier than earlier ones,
            # scaled off the measured overall rate.
            scale = DEFAULT_ODDS[stage] / DEFAULT_ODDS["quoted"]
            odds[stage] = round(min(0.95, base * scale), 4)
            measured[stage] = True
        else:
            odds[stage] = DEFAULT_ODDS[stage]
            measured[stage] = False
    return odds, measured


def evaluate(
    opportunities: Sequence[Opportunity],
    spend: Sequence[Spend] = (),
    stock_rows: Sequence[dict] = (),
    cash_headroom: float | None = None,
    thresholds: Thresholds | None = None,
    as_of: date | None = None,
) -> MarketingResult:
    thresholds = thresholds or Thresholds()
    as_of = as_of or date.today()
    odds, measured = measured_odds(opportunities)

    # What is on the shelf, and how long a replacement takes. Kept per
    # company and per branch rather than summed into one number: stock in
    # another company is not available to this one without an inter-company
    # sale, and stock in another branch is a transfer, not a shelf. Summing
    # the lot told a Karachi tender it was covered by stock in Islamabad
    # belonging to a different legal entity.
    position: dict = {}
    for r in stock_rows:
        key = r.get("item_key")
        if key is None:
            continue
        cur = position.setdefault(key, {"by_place": {}, "lead_time": 0})
        place = (r.get("entity_id", ""), r.get("location_id", ""))
        cur["by_place"][place] = (cur["by_place"].get(place, 0.0)
                                  + (r.get("on_hand", 0) or 0))
        cur["lead_time"] = max(cur["lead_time"], r.get("lead_time_days", 0) or 0)

    rows, at_risk = [], []
    for o in opportunities:
        if o.stage not in OPEN_STAGES:
            continue
        chance = odds.get(o.stage, 0.0)
        weighted = round(o.value * chance, 2)
        days = (o.expected_close - as_of).days if o.expected_close else None
        age = (as_of - o.opened).days if o.opened else None

        supply, note = _supply(o, position, days)
        if supply == "cannot supply in time":
            at_risk.append({
                "opportunity_id": o.opportunity_id,
                "customer_name": o.customer_name,
                "item_key": o.item_key, "value": o.value,
                "days_to_close": days,
                "lead_time_days": position.get(o.item_key, {}).get("lead_time"),
            })

        bits = [f"{int(chance * 100)}% at this stage"]
        if not measured.get(o.stage, False):
            bits.append("an assumed rate, not a measured one")
        if note:
            bits.append(note)
        if age and age > 120:
            bits.append(f"open {age} days, which is long enough to question")

        rows.append(PipelineRow(
            opportunity_id=o.opportunity_id, customer_key=o.customer_key,
            customer_name=o.customer_name, entity_id=o.entity_id,
            location_id=o.location_id, stage=o.stage, value=round(o.value, 2),
            odds=chance, weighted=weighted,
            odds_measured=measured.get(o.stage, False),
            days_to_close=days,
            expected_close=o.expected_close.isoformat() if o.expected_close else "",
            item_key=o.item_key, quantity=o.quantity, supply=supply,
            source=o.source, owner=o.owner, age_days=age,
            reason="; ".join(bits) + ".",
        ))

    rows.sort(key=lambda r: -r.weighted)
    open_value = round(sum(r.value for r in rows), 2)
    weighted_value = round(sum(r.weighted for r in rows), 2)
    won = [o for o in opportunities if o.stage == "won"]
    lost = [o for o in opportunities if o.stage == "lost"]
    won_value = round(sum(o.value for o in won), 2)
    lost_value = round(sum(o.value for o in lost), 2)
    closed_n = len(won) + len(lost)
    win_rate = round(len(won) / closed_n, 4) if closed_n else None

    total_spend = round(sum(s.amount for s in spend), 2)
    cost_per_win = round(total_spend / len(won), 2) if won else None
    per_rupee = round(total_spend / won_value, 4) if won_value else None

    by_stage, by_source = {}, {}
    for r in rows:
        st = by_stage.setdefault(r.stage, {"count": 0, "value": 0.0,
                                           "weighted": 0.0})
        st["count"] += 1
        st["value"] = round(st["value"] + r.value, 2)
        st["weighted"] = round(st["weighted"] + r.weighted, 2)
    for o in opportunities:
        src = by_source.setdefault(o.source or "unrecorded",
                                   {"count": 0, "value": 0.0, "won": 0})
        src["count"] += 1
        src["value"] = round(src["value"] + o.value, 2)
        if o.stage == "won":
            src["won"] += 1

    unfundable = 0.0
    if cash_headroom is not None:
        # What the pipeline would cost to serve, roughly, against what the
        # cash view says is spare. Stock has to be bought before it is sold.
        cost = weighted_value * (1 - 0.35)
        unfundable = round(max(0.0, cost - max(cash_headroom, 0)), 2)

    return MarketingResult(
        rows=tuple(rows), open_count=len(rows), open_value=open_value,
        weighted_value=weighted_value, won_value=won_value,
        lost_value=lost_value, win_rate=win_rate,
        win_rate_measured=closed_n >= MIN_FOR_MEASURED_ODDS,
        spend=total_spend, cost_per_win=cost_per_win,
        spend_per_rupee_won=per_rupee, by_stage=by_stage, by_source=by_source,
        at_risk_supply=tuple(at_risk), unfundable=unfundable,
        reason=_reason(rows, open_value, weighted_value, win_rate,
                       closed_n, total_spend, cost_per_win, at_risk),
        caveat=(
            "Stage odds are measured only once enough has closed to measure "
            "them; until then they are assumptions and every weighted figure "
            "that used one says so. Weighted pipeline is not revenue and "
            "should never be added to it."
        ),
    )


def _supply(o, position, days):
    """Can this be served, and from where.

    Three different answers, because they mean three different things to
    whoever is working the deal: it is on your own shelf, it is on a
    sister branch's shelf and needs a transfer, or it has to be imported
    and the lead time decides whether that is possible before the close.
    """
    if not o.item_key:
        return "not linked to an item", ""
    held = position.get(o.item_key)
    if held is None:
        return "item not in stock data", (
            "the item on this deal is not in the stock file")

    want = o.quantity or 0
    here = held["by_place"].get((o.entity_id, o.location_id), 0.0)
    same_company = sum(
        qty for (ent, _loc), qty in held["by_place"].items()
        if ent == o.entity_id)

    if want and here >= want:
        return "in stock", "the stock to serve it is on this branch's shelf"
    if want and same_company >= want:
        return "in stock at another branch", (
            f"{here:.0f} here against {want:.0f} needed, but the company "
            f"holds {same_company:.0f} across its branches: this is a "
            f"transfer, not an import")

    lead = held["lead_time"] or 0
    if days is not None and lead and days < lead:
        return "cannot supply in time", (
            f"closing in {days} days against a {lead} day lead time, with "
            f"{same_company:.0f} on hand across the company")
    return "needs ordering", "it will have to be ordered in"


def _money(v):
    if abs(v) >= 1_000_000:
        return f"PKR {v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"PKR {v / 1_000:.0f}K"
    return f"PKR {v:,.0f}"


def _reason(rows, open_value, weighted, win_rate, closed_n, spend,
            cost_per_win, at_risk):
    if not rows:
        # Nothing open does not mean nothing to say: a business with a
        # closed history and an empty pipeline is exactly the one that needs
        # telling what its win rate was.
        if not closed_n:
            return "Nothing open in the pipeline, and nothing closed yet."
        rate = (f"{win_rate:.0%} of the {closed_n} closed deals were won"
                if closed_n >= MIN_FOR_MEASURED_ODDS else
                f"only {closed_n} deals have closed, too few to call that a "
                f"win rate")
        return (f"Nothing open in the pipeline. Historically {rate}.")
    bits = [
        f"{len(rows)} open worth {_money(open_value)}, "
        f"{_money(weighted)} weighted."
    ]
    if win_rate is not None:
        bits.append(
            f"{win_rate:.0%} of the {closed_n} closed deals were won."
            if closed_n >= MIN_FOR_MEASURED_ODDS else
            f"Only {closed_n} deals have closed, too few to call that a win rate."
        )
    if spend and cost_per_win:
        bits.append(f"{_money(spend)} of marketing spend, {_money(cost_per_win)} a win.")
    if at_risk:
        worst = max(at_risk, key=lambda a: a["value"])
        bits.append(
            f"{len(at_risk)} of them cannot be supplied in time, the largest "
            f"being {_money(worst['value'])} at {worst['customer_name']}."
        )
    return " ".join(bits)
