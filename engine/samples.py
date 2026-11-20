"""Sample data pack: canonical datasets, templates, and the column registry.

One registry, three uses. The CSV headers you download, the example row in a
blank template, and the parser that reads your file back are all generated from
`DATASETS` below. That is deliberate: when a template and its parser are
written separately they drift, and the first symptom is a customer's upload
being silently rejected.

The generated figures are invented. They are shaped to exercise every branch
of the engine rather than to resemble any real company:

  - a redeployment pair: excess at Karachi against a stockout in Lahore
  - an expiring batch that is better moved than written off
  - intermittent spares, so cycle service level is used rather than fill rate
  - one hospital owing money in two entities, which no single ledger shows
  - an inter-company invoice, which must not be consolidated
  - a placement whose reagent pull will never repay its capital
  - service jobs with unbilled hours and parts fitted free

Generation is seeded, so the same pack is produced every time and two people
comparing results are looking at the same numbers.
"""

from __future__ import annotations

import csv
import io
import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional, Tuple

SEED = 20261120
AS_OF = date(2026, 11, 20)

# Two legally separate companies under one owner, three branches between them.
# This is the shape that makes consolidation and inter-company netting real.
ENTITIES = {
    "DX": "Diagnostix (Pvt) Ltd",
    "MS": "MedSupply (Pvt) Ltd",
}
LOCATIONS = [
    ("DX", "KHI", "Karachi warehouse"),
    ("DX", "LHR", "Lahore branch"),
    ("MS", "ISB", "Islamabad branch"),
]


# --------------------------------------------------------------- registry


@dataclass(frozen=True)
class Column:
    name: str
    example: str
    note: str
    required: bool = False


@dataclass(frozen=True)
class Dataset:
    id: str
    filename: str
    title: str
    bucket: str          # the upload bucket this file belongs to
    description: str
    columns: Tuple[Column, ...]
    rows: Callable[[], List[dict]]

    @property
    def header(self) -> List[str]:
        return [c.name for c in self.columns]


def _csv(header: List[str], rows: List[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in header})
    return buf.getvalue()


# ----------------------------------------------------------------- items
#
# profile: ("smooth", monthly_mean, cv) or ("intermittent", size, p_zero)
# stock:   location -> (on_hand, open_po, expiry_in_days or None)

_ITEMS: Tuple[dict, ...] = (
    {
        "key": "HEM-RGT-01", "desc": "Haematology reagent pack", "cat": "Reagent",
        "cost": 5_000.0, "lead": 75, "lead_sd": 14, "clinical": True,
        "profile": ("smooth", 120, 0.12),
        # The redeployment pair. KHI is 12 months deep, LHR runs out.
        "stock": {"KHI": (1_500, 0, None), "LHR": (12, 0, None), "ISB": (430, 0, None)},
    },
    {
        "key": "BGA-CRT-05", "desc": "Blood gas cartridge", "cat": "Reagent",
        "cost": 12_000.0, "lead": 60, "lead_sd": 10, "clinical": True,
        "profile": ("smooth", 60, 0.15),
        # Expiring batch at KHI that LHR could consume before it dies.
        "stock": {"KHI": (560, 0, 45), "LHR": (8, 0, None), "ISB": (190, 0, None)},
    },
    {
        "key": "HEM-CTL-02", "desc": "Haematology control set", "cat": "Control",
        "cost": 8_500.0, "lead": 70, "lead_sd": 12, "clinical": False,
        "profile": ("smooth", 24, 0.20),
        "stock": {"KHI": (26, 0, None), "LHR": (18, 12, None), "ISB": (60, 0, None)},
    },
    {
        "key": "TEG-CUP-03", "desc": "Thromboelastography cup", "cat": "Consumable",
        "cost": 1_800.0, "lead": 55, "lead_sd": 9, "clinical": True,
        "profile": ("smooth", 300, 0.18),
        "stock": {"KHI": (310, 0, None), "LHR": (2_600, 0, None), "ISB": (140, 0, None)},
    },
    {
        "key": "TEG-RGT-04", "desc": "TEG activator reagent", "cat": "Reagent",
        "cost": 9_400.0, "lead": 65, "lead_sd": 11, "clinical": True,
        "profile": ("smooth", 55, 0.22),
        "stock": {"KHI": (45, 0, None), "LHR": (70, 0, None), "ISB": (38, 0, None)},
    },
    {
        "key": "BGA-ELC-06", "desc": "Blood gas electrode", "cat": "Spare",
        "cost": 46_000.0, "lead": 90, "lead_sd": 21, "clinical": False,
        "profile": ("intermittent", 3, 0.6),
        "stock": {"KHI": (14, 0, None), "LHR": (4, 0, None), "ISB": (9, 0, None)},
    },
    {
        "key": "PFT-FLT-07", "desc": "Spirometer filter", "cat": "Consumable",
        "cost": 320.0, "lead": 45, "lead_sd": 7, "clinical": False,
        "profile": ("smooth", 90, 0.25),
        "stock": {"KHI": (1_900, 0, None), "LHR": (210, 0, None), "ISB": (260, 0, None)},
    },
    {
        "key": "DX-STR-08", "desc": "Urine strip bottle", "cat": "Consumable",
        "cost": 2_400.0, "lead": 50, "lead_sd": 8, "clinical": False,
        "profile": ("smooth", 140, 0.20),
        "stock": {"KHI": (120, 60, None), "LHR": (90, 0, None), "ISB": (14, 0, None)},
    },
    {
        "key": "SPR-PMP-09", "desc": "Peristaltic pump assembly", "cat": "Spare",
        "cost": 88_000.0, "lead": 110, "lead_sd": 28, "clinical": False,
        "profile": ("intermittent", 2, 0.72),
        "stock": {"KHI": (3, 0, None), "LHR": (1, 0, None), "ISB": (6, 0, None)},
    },
    {
        "key": "SPR-LMP-10", "desc": "Photometer lamp", "cat": "Spare",
        "cost": 34_000.0, "lead": 85, "lead_sd": 18, "clinical": False,
        "profile": ("intermittent", 2, 0.55),
        "stock": {"KHI": (22, 0, None), "LHR": (3, 0, None), "ISB": (11, 0, None)},
    },
    {
        "key": "CHM-RGT-11", "desc": "Chemistry reagent kit", "cat": "Reagent",
        "cost": 6_700.0, "lead": 68, "lead_sd": 13, "clinical": True,
        "profile": ("smooth", 200, 0.14),
        "stock": {"KHI": (780, 0, None), "LHR": (520, 0, None), "ISB": (95, 0, None)},
    },
    {
        "key": "CHM-CAL-12", "desc": "Chemistry calibrator", "cat": "Control",
        "cost": 15_500.0, "lead": 72, "lead_sd": 15, "clinical": False,
        "profile": ("smooth", 18, 0.30),
        "stock": {"KHI": (16, 0, 70), "LHR": (9, 0, None), "ISB": (24, 0, None)},
    },
)

_PERIODS: Tuple[str, ...] = (
    "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05",
    "2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11",
)

# Demand is split across branches so each one has its own profile and policy.
_LOC_SHARE = {"KHI": 0.50, "LHR": 0.33, "ISB": 0.17}


def inventory_rows() -> List[dict]:
    out: List[dict] = []
    for ent, loc, _ in LOCATIONS:
        for it in _ITEMS:
            on_hand, open_po, expiry_in = it["stock"][loc]
            out.append(
                {
                    "entity_id": ent,
                    "location_id": loc,
                    "item_key": it["key"],
                    "description": it["desc"],
                    "category": it["cat"],
                    "on_hand": on_hand,
                    "open_po": open_po,
                    "unit_cost": f'{it["cost"]:.2f}',
                    "lead_time_days": it["lead"],
                    "lead_time_days_std": it["lead_sd"],
                    "is_clinical_critical": "yes" if it["clinical"] else "no",
                    "expiry_date": (
                        (AS_OF + timedelta(days=expiry_in)).isoformat()
                        if expiry_in
                        else ""
                    ),
                }
            )
    return out


def demand_rows() -> List[dict]:
    rng = random.Random(SEED)
    out: List[dict] = []
    for ent, loc, _ in LOCATIONS:
        share = _LOC_SHARE[loc]
        for it in _ITEMS:
            kind = it["profile"][0]
            for period in _PERIODS:
                if kind == "smooth":
                    _, mean, cv = it["profile"]
                    local = mean * share
                    q = max(0.0, rng.gauss(local, local * cv))
                    qty = round(q)
                else:
                    _, size, p_zero = it["profile"]
                    if rng.random() < p_zero:
                        qty = 0
                    else:
                        qty = max(1, round(rng.gauss(size * share * 3, size * 0.5)))
                out.append(
                    {
                        "entity_id": ent,
                        "location_id": loc,
                        "item_key": it["key"],
                        "period": period,
                        "quantity": qty,
                    }
                )
    return out


# ----------------------------------------------------------- receivables

_CUSTOMERS: Tuple[dict, ...] = (
    # key, name, public sector, entities it buys through
    {"key": "PIMS", "name": "Public Hospital A (PIMS)", "public": True, "ents": ["DX", "MS"]},
    {"key": "JPMC", "name": "Public Hospital B (JPMC)", "public": True, "ents": ["DX"]},
    {"key": "SVCH", "name": "Services Hospital", "public": True, "ents": ["MS"]},
    {"key": "AKUH", "name": "Aga Private Hospital", "public": False, "ents": ["DX"]},
    {"key": "SHFK", "name": "Shifa Private Clinic", "public": False, "ents": ["DX", "MS"]},
    {"key": "LABX", "name": "Metro Pathology Lab", "public": False, "ents": ["MS"]},
    {"key": "CMH-R", "name": "Garrison Hospital", "public": True, "ents": ["DX"]},
    {"key": "DIAGC", "name": "City Diagnostic Centre", "public": False, "ents": ["MS"]},
)

# Public-sector terms in this market routinely run past 180 days, so the
# generated ageing is deliberately ugly at the top end.
_AGE_PROFILE = {
    True: (40, 300),    # public: 40 to 300 days since invoice
    False: (5, 120),    # private: 5 to 120
}


def receivables_rows() -> List[dict]:
    rng = random.Random(SEED + 1)
    out: List[dict] = []
    n = 1

    for cust in _CUSTOMERS:
        for ent in cust["ents"]:
            loc = "KHI" if ent == "DX" else "ISB"
            count = rng.randint(2, 5)
            for _ in range(count):
                lo, hi = _AGE_PROFILE[cust["public"]]
                age = rng.randint(lo, hi)
                issued = AS_OF - timedelta(days=age)
                terms = 90 if cust["public"] else 30
                amount = round(rng.uniform(300_000, 4_500_000), -3)
                # Older public invoices are usually part-paid, never cleared.
                paid = (
                    round(amount * rng.uniform(0.1, 0.45), -3)
                    if cust["public"] and age > 180 and rng.random() < 0.6
                    else 0
                )
                out.append(
                    {
                        "entity_id": ent,
                        "location_id": loc,
                        "invoice_no": f"INV-{n:04d}",
                        "customer_key": cust["key"],
                        "customer_name": cust["name"],
                        "invoice_date": issued.isoformat(),
                        "due_date": (issued + timedelta(days=terms)).isoformat(),
                        "amount": f"{amount:.2f}",
                        "amount_paid": f"{paid:.2f}",
                        "is_public_sector": "yes" if cust["public"] else "no",
                        "is_intercompany": "no",
                    }
                )
                n += 1

    # One inter-company invoice. Consolidating this double counts it, so the
    # engine must exclude it from the group book and say so.
    out.append(
        {
            "entity_id": "DX",
            "location_id": "KHI",
            "invoice_no": f"INV-{n:04d}",
            "customer_key": "MS",
            "customer_name": "MedSupply (Pvt) Ltd",
            "invoice_date": (AS_OF - timedelta(days=45)).isoformat(),
            "due_date": (AS_OF - timedelta(days=15)).isoformat(),
            "amount": "2750000.00",
            "amount_paid": "0.00",
            "is_public_sector": "no",
            "is_intercompany": "yes",
        }
    )
    return out


# --------------------------------------------------------------- machines

_MACHINES: Tuple[dict, ...] = (
    # Healthy: good pull, ahead of the payback target.
    {"key": "AN-5100-01", "model": "Haematology Analyser 5100", "cust": "AKUH",
     "ent": "DX", "loc": "KHI", "placed": 980, "capital": 3_200_000, "months": 60,
     "revenue": 11_800_000, "margin": 0.36, "exp_tests": 1_100, "act_tests": 1_180,
     "down": 4, "service": 240_000, "per_test": 410},
    # The expensive mistake: volume never materialised.
    {"key": "AN-7200-02", "model": "Chemistry Analyser 7200", "cust": "SVCH",
     "ent": "MS", "loc": "ISB", "placed": 910, "capital": 6_400_000, "months": 48,
     "revenue": 2_100_000, "margin": 0.30, "exp_tests": 900, "act_tests": 210,
     "down": 38, "service": 520_000, "per_test": 360},
    {"key": "BG-300-03", "model": "Blood Gas 300", "cust": "PIMS",
     "ent": "DX", "loc": "KHI", "placed": 640, "capital": 1_900_000, "months": 36,
     "revenue": 4_300_000, "margin": 0.34, "exp_tests": 600, "act_tests": 520,
     "down": 11, "service": 180_000, "per_test": 290},
    {"key": "BG-300-04", "model": "Blood Gas 300", "cust": "SHFK",
     "ent": "MS", "loc": "ISB", "placed": 420, "capital": 1_900_000, "months": 36,
     "revenue": 2_600_000, "margin": 0.34, "exp_tests": 600, "act_tests": 480,
     "down": 6, "service": 95_000, "per_test": 290},
    # Just placed, nothing back yet. Correctly stalled, not alarming.
    {"key": "TEG-50-05", "model": "TEG 50", "cust": "CMH-R",
     "ent": "DX", "loc": "LHR", "placed": 75, "capital": 2_700_000, "months": 36,
     "revenue": 180_000, "margin": 0.38, "exp_tests": 300, "act_tests": 120,
     "down": 0, "service": 40_000, "per_test": 520},
    # Running, but service cost is eating the margin.
    {"key": "PFT-20-06", "model": "Spirometer Pro 20", "cust": "LABX",
     "ent": "MS", "loc": "ISB", "placed": 1_240, "capital": 900_000, "months": 60,
     "revenue": 1_900_000, "margin": 0.28, "exp_tests": 400, "act_tests": 330,
     "down": 22, "service": 610_000, "per_test": 140},
    {"key": "AN-5100-07", "model": "Haematology Analyser 5100", "cust": "JPMC",
     "ent": "DX", "loc": "KHI", "placed": 1_510, "capital": 3_200_000, "months": 60,
     "revenue": 15_400_000, "margin": 0.36, "exp_tests": 1_100, "act_tests": 1_020,
     "down": 17, "service": 740_000, "per_test": 410},
    {"key": "CH-900-08", "model": "Chemistry 900", "cust": "DIAGC",
     "ent": "MS", "loc": "ISB", "placed": 300, "capital": 2_200_000, "months": 48,
     "revenue": 1_450_000, "margin": 0.32, "exp_tests": 500, "act_tests": 420,
     "down": 3, "service": 70_000, "per_test": 330},
)

_CUST_NAME = {c["key"]: c["name"] for c in _CUSTOMERS}


def machine_rows() -> List[dict]:
    out: List[dict] = []
    for m in _MACHINES:
        out.append(
            {
                "entity_id": m["ent"],
                "location_id": m["loc"],
                "machine_key": m["key"],
                "model": m["model"],
                "customer_key": m["cust"],
                "customer_name": _CUST_NAME.get(m["cust"], m["cust"]),
                "placed_date": (AS_OF - timedelta(days=m["placed"])).isoformat(),
                "capital_cost": f'{m["capital"]:.2f}',
                "contract_months": m["months"],
                "reagent_revenue_to_date": f'{m["revenue"]:.2f}',
                "reagent_margin_pct": f'{m["margin"]:.2f}',
                "expected_monthly_tests": m["exp_tests"],
                "actual_monthly_tests": m["act_tests"],
                "downtime_days_ytd": m["down"],
                "service_cost_to_date": f'{m["service"]:.2f}',
                "revenue_per_test": f'{m["per_test"]:.2f}',
            }
        )
    return out


# ---------------------------------------------------------------- service


def service_rows() -> List[dict]:
    rng = random.Random(SEED + 2)
    out: List[dict] = []
    n = 1
    for m in _MACHINES:
        # Machines that are down a lot generate more jobs, and more repeats.
        count = 2 + int(m["down"] / 8)
        prev_fault: Optional[str] = None
        for _ in range(count):
            opened = AS_OF - timedelta(days=rng.randint(5, 330))
            open_job = rng.random() < 0.15
            closed = (
                "" if open_job else (opened + timedelta(days=rng.randint(1, 14))).isoformat()
            )
            warranty = rng.random() < 0.2
            hours = round(rng.uniform(2, 16), 1)
            # The wastage: hours worked that nobody invoiced.
            billed = 0.0 if warranty else round(hours * rng.uniform(0.45, 1.0), 1)
            parts = round(rng.uniform(0, 420_000), -3)
            charged = 0.0 if warranty else round(parts * rng.uniform(0.3, 1.0), -3)
            repeat = prev_fault is not None and rng.random() < 0.45
            prev_fault = m["key"]
            out.append(
                {
                    "entity_id": m["ent"],
                    "location_id": m["loc"],
                    "job_no": f"JOB-{n:04d}",
                    "machine_key": m["key"],
                    "opened_date": opened.isoformat(),
                    "closed_date": closed,
                    "labour_hours": hours,
                    "labour_hours_billed": billed,
                    "parts_cost": f"{parts:.2f}",
                    "parts_charged": f"{charged:.2f}",
                    "is_warranty": "yes" if warranty else "no",
                    "is_repeat_visit": "yes" if repeat else "no",
                    "downtime_days": rng.randint(0, 9),
                }
            )
            n += 1
    return out


# ---------------------------------------------------------------- DATASETS

# ------------------------------------------------------------ commitments
#
# The outflows a cash forecast cannot ignore. A 13-week view built from
# receivables and purchase orders alone is fiction: the letter of credit
# settles, the bank is repaid and the salaries go out whatever the stock
# position says.

_COMMITMENTS: Tuple[dict, ...] = (
    {"label": "Payroll", "kind": "payroll", "ent": "DX", "loc": "KHI",
     "amount": 4_200_000, "day": 1, "recurs": 30},
    {"label": "Payroll", "kind": "payroll", "ent": "MS", "loc": "ISB",
     "amount": 2_100_000, "day": 1, "recurs": 30},
    {"label": "Warehouse rent", "kind": "rent", "ent": "DX", "loc": "KHI",
     "amount": 900_000, "day": 5, "recurs": 30},
    {"label": "Branch rent", "kind": "rent", "ent": "MS", "loc": "ISB",
     "amount": 450_000, "day": 5, "recurs": 30},
    # The bank facility that finances the import gap.
    {"label": "Bank facility repayment", "kind": "loan_repayment", "ent": "DX",
     "loc": "KHI", "amount": 6_500_000, "day": 10, "recurs": 30},
    {"label": "Equipment finance", "kind": "loan_repayment", "ent": "MS",
     "loc": "ISB", "amount": 1_750_000, "day": 15, "recurs": 30},
    # One-off letters of credit already opened against shipments in transit.
    {"label": "LC 4471 settlement, analyser reagents", "kind": "lc_settlement",
     "ent": "DX", "loc": "KHI", "amount": 14_800_000, "day": 18, "recurs": 0},
    {"label": "LC 4489 settlement, blood gas cartridges", "kind": "lc_settlement",
     "ent": "DX", "loc": "KHI", "amount": 7_300_000, "day": 46, "recurs": 0},
    {"label": "LC 4502 settlement, chemistry kits", "kind": "lc_settlement",
     "ent": "MS", "loc": "ISB", "amount": 5_900_000, "day": 61, "recurs": 0},
    {"label": "Advance tax instalment", "kind": "tax", "ent": "DX", "loc": "KHI",
     "amount": 3_400_000, "day": 74, "recurs": 0},
    {"label": "Spare parts supplier, net 30", "kind": "supplier", "ent": "DX",
     "loc": "KHI", "amount": 2_250_000, "day": 27, "recurs": 0},
)


def commitment_rows() -> List[dict]:
    out: List[dict] = []
    for c in _COMMITMENTS:
        out.append(
            {
                "entity_id": c["ent"],
                "location_id": c["loc"],
                "label": c["label"],
                "kind": c["kind"],
                "due_date": (AS_OF + timedelta(days=c["day"])).isoformat(),
                "amount": f'{c["amount"]:.2f}',
                "is_recurring": "yes" if c["recurs"] else "no",
                "recurs_every_days": c["recurs"] or "",
            }
        )
    return out


# ------------------------------------------------- finance, payroll, pipeline

# A year that articulates. The closing cash is not a figure somebody liked
# the look of: it is what the cash flow produces, 4.0M opening less the
# 160K the business actually consumed. If these numbers are edited, the
# product's own checks will say so, which is the point of shipping them.
_TRIAL_BALANCE = [
    ("DX", "4000", "Sales", "revenue", 0, 48_000_000),
    ("DX", "5000", "Cost of sales", "cogs", 0, 31_200_000),
    ("DX", "6000", "Salaries and benefits", "payroll", 0, 7_800_000),
    ("DX", "6100", "Rent, utilities and running", "opex", 0, 3_600_000),
    ("DX", "6200", "Depreciation", "depreciation", 0, 1_200_000),
    ("DX", "7000", "Bank markup", "finance_cost", 0, 900_000),
    ("DX", "8000", "Income tax", "tax", 0, 960_000),
    ("DX", "1000", "Bank and cash", "cash", 4_000_000, 3_840_000),
    ("DX", "1100", "Trade debtors", "receivables", 12_000_000, 14_000_000),
    ("DX", "1200", "Stock in trade", "inventory", 9_000_000, 10_500_000),
    ("DX", "1500", "Equipment and vehicles", "fixed_assets",
     14_000_000, 16_000_000),
    ("DX", "1510", "Accumulated depreciation", "accumulated_depreciation",
     5_000_000, 6_200_000),
    ("DX", "2000", "Trade creditors", "payables", 8_000_000, 9_300_000),
    ("DX", "2100", "Running finance", "short_term_debt", 6_000_000, 6_500_000),
    ("DX", "3000", "Share capital", "equity", 5_000_000, 5_000_000),
    ("DX", "3100", "Retained earnings", "retained_earnings",
     15_000_000, 17_340_000),
]


def trial_balance_rows() -> List[dict]:
    return [
        {
            "entity_id": e, "account_code": code, "account_name": name,
            "statement_line": line,
            "opening_balance": f"{opening:.2f}",
            "closing_balance": f"{closing:.2f}",
        }
        for e, code, name, line, opening, closing in _TRIAL_BALANCE
    ]


_STAFF = [
    ("EMP-001", "Ayesha Khan", "DX", "KHI", "Sales", 120_000, 60_000, 12_000,
     0.08, 0.08, 980, "no"),
    ("EMP-002", "Bilal Ahmed", "DX", "KHI", "Warehouse", 30_000, 12_000,
     3_000, 0.0, 0.0, 1_400, "yes"),
    ("EMP-003", "Hira Siddiqui", "DX", "KHI", "Service", 85_000, 35_000,
     8_500, 0.08, 0.08, 760, "no"),
    ("EMP-004", "Usman Tariq", "DX", "LHR", "Sales", 95_000, 45_000, 9_500,
     0.08, 0.08, 540, "no"),
    ("EMP-005", "Nadia Aslam", "DX", "LHR", "Finance", 110_000, 40_000,
     11_000, 0.08, 0.08, 1_120, "no"),
    ("EMP-006", "Faisal Mehmood", "MS", "ISB", "Service", 70_000, 28_000,
     7_000, 0.0, 0.0, 300, "yes"),
    ("EMP-007", "Sana Javed", "MS", "ISB", "Sales", 60_000, 24_000, 6_000,
     0.0, 0.0, 180, "yes"),
    ("EMP-008", "Imran Qureshi", "DX", "KHI", "Management", 320_000, 160_000,
     32_000, 0.10, 0.10, 2_200, "no"),
]


def employee_rows() -> List[dict]:
    return [
        {
            "employee_id": eid, "name": name, "entity_id": ent,
            "location_id": loc, "department": dept,
            "basic_monthly": f"{basic:.2f}",
            "allowances_monthly": f"{allow:.2f}",
            "medical_monthly": f"{med:.2f}",
            "employer_pf_rate": f"{er_pf}",
            "employee_pf_rate": f"{ee_pf}",
            "joined": (AS_OF - timedelta(days=days)).isoformat(),
            "left": "",
            "eobi_registered": "yes",
            "social_security": ss,
            "is_active": "yes",
        }
        for (eid, name, ent, loc, dept, basic, allow, med, er_pf, ee_pf,
             days, ss) in _STAFF
    ]


_PIPELINE = [
    ("OPP-001", "PIMS", "tender_submitted", 8_400_000, 30, "HEM-RGT-01", 400,
     "tender", "Ayesha Khan", 111, "DX", "KHI"),
    ("OPP-002", "AKUH", "quoted", 3_200_000, 74, "CHM-RGT-11", 120,
     "referral", "Usman Tariq", 46, "DX", "KHI"),
    ("OPP-003", "SHFK", "qualified", 2_100_000, 95, "TEG-RGT-04", 60,
     "conference", "Ayesha Khan", 60, "DX", "LHR"),
    ("OPP-004", "SVCH", "lead", 1_500_000, 132, "", 0, "conference",
     "Usman Tariq", 172, "DX", "LHR"),
    ("OPP-005", "CMH", "quoted", 5_600_000, 52, "BGA-CRT-05", 200, "tender",
     "Sana Javed", 38, "MS", "ISB"),
    ("OPP-006", "LGH", "won", 4_200_000, -20, "HEM-CTL-02", 150, "tender",
     "Ayesha Khan", 150, "DX", "KHI"),
    ("OPP-007", "JPMC", "won", 1_800_000, -45, "CHM-CAL-12", 80, "referral",
     "Usman Tariq", 120, "DX", "LHR"),
    ("OPP-008", "NICVD", "won", 2_600_000, -62, "TEG-CUP-03", 300, "tender",
     "Sana Javed", 140, "MS", "ISB"),
    ("OPP-009", "AKUH", "won", 900_000, -80, "DX-STR-08", 50, "cold",
     "Ayesha Khan", 95, "DX", "KHI"),
    ("OPP-010", "DUHS", "lost", 3_100_000, -28, "HEM-RGT-01", 180, "tender",
     "Usman Tariq", 130, "DX", "KHI"),
    ("OPP-011", "SIUT", "lost", 1_250_000, -51, "CHM-RGT-11", 70,
     "conference", "Sana Javed", 101, "MS", "ISB"),
    ("OPP-012", "PIMS", "lost", 2_400_000, -73, "SPR-PMP-09", 20, "tender",
     "Ayesha Khan", 160, "DX", "KHI"),
    ("OPP-013", "AKUH", "won", 3_400_000, -95, "TEG-RGT-04", 90, "referral",
     "Hira Siddiqui", 180, "DX", "KHI"),
]


def opportunity_rows() -> List[dict]:
    out: List[dict] = []
    for (oid, cust, stage, value, close_in, item, qty, source, owner,
         age, ent, loc) in _PIPELINE:
        closed = stage in ("won", "lost")
        out.append({
            "opportunity_id": oid,
            "entity_id": ent,
            "location_id": loc,
            "customer_key": cust,
            "customer_name": _CUST_NAME.get(cust, cust),
            "stage": stage,
            "value": f"{value:.2f}",
            "expected_close": (AS_OF + timedelta(days=close_in)).isoformat(),
            "item_key": item,
            "quantity": qty,
            "source": source,
            "owner": owner,
            "opened": (AS_OF - timedelta(days=age)).isoformat(),
            "closed": (AS_OF + timedelta(days=close_in)).isoformat()
                      if closed else "",
        })
    return out


_SPEND = [
    ("DX", "MEDEXPO Karachi stand", "conference", 82, 850_000),
    ("DX", "Demonstration analyser, Karachi", "demo_unit", 160, 1_200_000),
    ("DX", "Sample reagents to hospitals", "samples", 45, 320_000),
    ("MS", "Islamabad diagnostics forum", "conference", 120, 410_000),
    ("DX", "Product brochures and manuals", "print", 200, 95_000),
    ("DX", "Search and social advertising", "digital", 30, 140_000),
]


def marketing_spend_rows() -> List[dict]:
    return [
        {
            "entity_id": ent, "label": label, "category": cat,
            "spent_on": (AS_OF - timedelta(days=days)).isoformat(),
            "amount": f"{amount:.2f}",
        }
        for ent, label, cat, days, amount in _SPEND
    ]


DATASETS: Dict[str, Dataset] = {
    "inventory": Dataset(
        id="inventory",
        filename="inventory.csv",
        title="Stock on hand",
        bucket="inventory",
        description=(
            "One row per item per location. The stock valuation or item listing "
            "export from your inventory system."
        ),
        columns=(
            Column("entity_id", "DX", "Which company. Must match across every file.", True),
            Column("location_id", "KHI", "Which branch or warehouse.", True),
            Column("item_key", "HEM-RGT-01", "Your SKU code. The join key for everything.", True),
            Column("description", "Haematology reagent pack", "Free text."),
            Column("category", "Reagent", "Your own grouping. Not used in any calculation."),
            Column("on_hand", "1500", "Units physically held. Not value.", True),
            Column("open_po", "0", "Units already ordered and not yet received."),
            Column("unit_cost", "5000.00", "Landed cost per unit, not selling price.", True),
            Column("lead_time_days", "75", "Order to receipt, in calendar days.", True),
            Column("lead_time_days_std", "14",
                   "How much the lead time varies. Leave 0 if unknown, but it "
                   "understates safety stock."),
            Column("is_clinical_critical", "yes",
                   "yes raises the service target to the clinical floor."),
            Column("expiry_date", "2027-01-04", "Earliest batch expiry. Blank if none."),
        ),
        rows=inventory_rows,
    ),
    "demand": Dataset(
        id="demand",
        filename="demand_history.csv",
        title="Demand history",
        bucket="sales",
        description=(
            "Units consumed per item per month per location. Twelve months is "
            "the minimum for classification to mean anything. Zero months must "
            "be included as zeros, not left out."
        ),
        columns=(
            Column("entity_id", "DX", "Which company.", True),
            Column("location_id", "KHI", "Which branch.", True),
            Column("item_key", "HEM-RGT-01", "Must match the inventory file exactly.", True),
            Column("period", "2026-10", "YYYY-MM.", True),
            Column("quantity", "58",
                   "Units issued or sold that month. A zero month is "
                   "information, so send the row with 0.", True),
        ),
        rows=demand_rows,
    ),
    "receivables": Dataset(
        id="receivables",
        filename="receivables.csv",
        title="Receivables",
        bucket="ar",
        description=(
            "One row per open invoice. The aged receivables detail export. "
            "Settled invoices may be included; they are netted out."
        ),
        columns=(
            Column("entity_id", "DX", "Which company's ledger.", True),
            Column("location_id", "KHI", "Which branch raised it."),
            Column("invoice_no", "INV-0001", "Your invoice number.", True),
            Column("customer_key", "PIMS",
                   "Customer code. Use the SAME code across entities or group "
                   "exposure cannot be seen.", True),
            Column("customer_name", "Public Hospital A (PIMS)", "Free text."),
            Column("invoice_date", "2026-02-14", "YYYY-MM-DD.", True),
            Column("due_date", "2026-05-15", "YYYY-MM-DD. Drives the ageing.", True),
            Column("amount", "2450000.00", "Invoice total.", True),
            Column("amount_paid", "0.00", "Received so far. The open amount is the difference."),
            Column("is_public_sector", "yes", "yes or no."),
            Column("is_intercompany", "no",
                   "yes excludes it from group totals. Get this wrong and the "
                   "consolidated book double counts."),
        ),
        rows=receivables_rows,
    ),
    "service": Dataset(
        id="service",
        filename="service_jobs.csv",
        title="Service jobs",
        bucket="service",
        description=(
            "One row per service or repair job. This is the file no ERP "
            "produces usefully, because it needs hours worked as well as hours "
            "billed."
        ),
        columns=(
            Column("entity_id", "DX", "Which company.", True),
            Column("location_id", "KHI", "Which branch the engineer works from."),
            Column("job_no", "JOB-0001", "Your job number.", True),
            Column("machine_key", "AN-5100-01",
                   "Must match the machines file to tie wastage to a placement.", True),
            Column("opened_date", "2026-09-02", "YYYY-MM-DD.", True),
            Column("closed_date", "2026-09-09", "Blank if still open."),
            Column("labour_hours", "11.5", "Hours actually worked.", True),
            Column("labour_hours_billed", "6.0",
                   "Hours invoiced. The gap is the wastage.", True),
            Column("parts_cost", "180000.00", "Landed cost of parts fitted."),
            Column("parts_charged", "60000.00", "Charged to the customer."),
            Column("is_warranty", "no",
                   "yes counts the job but never calls it wastage."),
            Column("is_repeat_visit", "no",
                   "yes means a return for the same fault."),
            Column("downtime_days", "3", "Days the machine was unusable."),
        ),
        rows=service_rows,
    ),
    "machines": Dataset(
        id="machines",
        filename="machines.csv",
        title="Placed equipment",
        bucket="inventory",
        description=(
            "One row per instrument placed with a customer and recovered "
            "through reagent margin. Usually kept in a spreadsheet rather than "
            "a system, because nothing models it."
        ),
        columns=(
            Column("entity_id", "DX", "Which company owns the asset.", True),
            Column("location_id", "KHI", "Which branch services it."),
            Column("machine_key", "AN-5100-01", "Your asset or serial code.", True),
            Column("model", "Haematology Analyser 5100", "Free text."),
            Column("customer_key", "AKUH", "Same customer code as receivables.", True),
            Column("customer_name", "Aga Private Hospital", "Free text."),
            Column("placed_date", "2024-03-15", "When it went in.", True),
            Column("capital_cost", "3200000.00", "What the instrument cost you.", True),
            Column("contract_months", "60", "Contract length. Payback must land inside it.", True),
            Column("reagent_revenue_to_date", "11800000.00",
                   "Consumable revenue earned on this machine so far.", True),
            Column("reagent_margin_pct", "0.36",
                   "Gross margin as a decimal. 0.36 means 36%.", True),
            Column("expected_monthly_tests", "1100",
                   "The volume it was placed against. The forecast that justified it."),
            Column("actual_monthly_tests", "1180", "What it actually runs."),
            Column("downtime_days_ytd", "4", "Days unusable this year."),
            Column("service_cost_to_date", "240000.00", "Your cost of servicing it."),
            Column("revenue_per_test", "410.00", "Used to price downtime."),
        ),
        rows=machine_rows,
    ),
    "commitments": Dataset(
        id="commitments",
        filename="commitments.csv",
        title="Committed payments",
        bucket="finance",
        description=(
            "Money already promised: letters of credit settling, bank and "
            "equipment finance repayments, payroll, rent and tax. The cash "
            "view is fiction without these, because they go out whatever the "
            "stock position says."
        ),
        columns=(
            Column("entity_id", "DX", "Which company pays it.", True),
            Column("location_id", "KHI", "Which branch, if it matters."),
            Column("label", "Bank facility repayment",
                   "How it should read on the cash week.", True),
            Column("kind", "loan_repayment",
                   "lc_settlement, loan_repayment, payroll, rent, tax, "
                   "supplier or other. Explains the week; changes no figure."),
            Column("due_date", "2026-11-30", "When the money leaves.", True),
            Column("amount", "6500000.00", "What leaves.", True),
            Column("is_recurring", "yes",
                   "yes repeats it across the horizon."),
            Column("recurs_every_days", "30",
                   "Required when is_recurring is yes. 30 for monthly."),
        ),
        rows=commitment_rows,
    ),
    "trial_balance": Dataset(
        id="trial_balance",
        filename="trial_balance.csv",
        title="Trial balance",
        bucket="finance",
        description=(
            "Your accounts, with an opening and a closing balance each. "
            "From these come the profit and loss, the balance sheet, and a "
            "cash flow derived from both rather than typed in. Amounts are "
            "positive in their natural sense: revenue positive, expenses "
            "positive, assets positive, liabilities positive. The "
            "statement_line column decides which figure each account "
            "becomes, so nothing is guessed from an account's name."
        ),
        columns=(
            Column("entity_id", "DX", "Which company's ledger.", True),
            Column("account_code", "4000", "Your own code.", True),
            Column("account_name", "Sales", "As it reads in your ledger.", True),
            Column("statement_line", "revenue",
                   "Where this account lands. One of: revenue, cogs, "
                   "payroll, opex, depreciation, finance_cost, other_income, "
                   "tax, cash, receivables, inventory, other_current_assets, "
                   "fixed_assets, accumulated_depreciation, payables, "
                   "short_term_debt, long_term_debt, other_liabilities, "
                   "tax_payable, equity, retained_earnings.", True),
            Column("opening_balance", "0.00",
                   "At the start of the period. Zero for profit and loss "
                   "accounts unless you are reporting year to date."),
            Column("closing_balance", "48000000.00",
                   "At the end of the period. For a profit and loss account "
                   "this is the period's amount.", True),
        ),
        rows=trial_balance_rows,
    ),
    "employees": Dataset(
        id="employees",
        filename="employees.csv",
        title="Payroll",
        bucket="finance",
        description=(
            "One row per employee. Tax is worked out under the Finance Act "
            "slabs in force on the as-of date, EOBI on the provincial "
            "minimum wage rather than on pay, social security where the "
            "worker is insured, and gratuity accrued monthly. Bonuses, "
            "arrears and final settlements are not modelled."
        ),
        columns=(
            Column("employee_id", "EMP-001", "Your own code.", True),
            Column("name", "Ayesha Khan", "For the payslip.", True),
            Column("entity_id", "DX", "Which company employs them.", True),
            Column("location_id", "KHI", "Which branch they work from."),
            Column("department", "Sales",
                   "Groups the cost. Sales, Service, Warehouse, Finance."),
            Column("basic_monthly", "120000.00",
                   "Basic pay a month. Gratuity and provident fund are "
                   "worked out on this.", True),
            Column("allowances_monthly", "60000.00",
                   "House rent, conveyance and the rest. Taxable."),
            Column("medical_monthly", "12000.00",
                   "Exempt up to a tenth of basic; anything above that is "
                   "taxable."),
            Column("employer_pf_rate", "0.08",
                   "Provident fund, as a decimal. 0.08 means 8% of basic."),
            Column("employee_pf_rate", "0.08", "Their share, same basis."),
            Column("joined", "2024-03-15", "Not paid before this date."),
            Column("left", "", "Blank while they are still with you."),
            Column("eobi_registered", "yes",
                   "yes charges EOBI at 5% employer and 1% employee of the "
                   "minimum wage."),
            Column("social_security", "no",
                   "yes charges provincial social security, employer only."),
            Column("is_active", "yes", "no leaves them out of the run."),
        ),
        rows=employee_rows,
    ),
    "opportunities": Dataset(
        id="opportunities",
        filename="opportunities.csv",
        title="Pipeline and tenders",
        bucket="marketing",
        description=(
            "What is being worked and what has closed. Weighted by each "
            "stage's own win rate once enough has closed to measure one, and "
            "checked against the stock position and the cash view: a tender "
            "closing inside the lead time of the item it needs is a "
            "disappointment with a date on it."
        ),
        columns=(
            Column("opportunity_id", "OPP-001", "Your own reference.", True),
            Column("entity_id", "DX", "Which company would book it.", True),
            Column("location_id", "KHI", "Which branch would serve it."),
            Column("customer_key", "PIMS",
                   "Same customer code as receivables, so exposure and "
                   "pipeline line up.", True),
            Column("customer_name", "Public Hospital A (PIMS)", "Free text."),
            Column("stage", "tender_submitted",
                   "lead, qualified, quoted, tender_submitted, won or lost.",
                   True),
            Column("value", "8400000.00", "What it is worth if won.", True),
            Column("expected_close", "2026-12-20", "When you expect to know."),
            Column("item_key", "HEM-RGT-01",
                   "What it would consume. Lets the supply check run."),
            Column("quantity", "400", "How many units."),
            Column("source", "tender",
                   "tender, referral, conference, cold. Groups the spend."),
            Column("owner", "Ayesha Khan", "Who is working it."),
            Column("opened", "2026-08-01", "When it started."),
            Column("closed", "", "Blank until it is won or lost."),
        ),
        rows=opportunity_rows,
    ),
    "marketing_spend": Dataset(
        id="marketing_spend",
        filename="marketing_spend.csv",
        title="Marketing spend",
        bucket="marketing",
        description=(
            "What was spent getting in front of customers. Priced against "
            "what was won, which is the only marketing figure a small "
            "business owner can act on."
        ),
        columns=(
            Column("entity_id", "DX", "Which company paid.", True),
            Column("label", "MEDEXPO Karachi stand", "What it was.", True),
            Column("category", "conference",
                   "conference, demo_unit, samples, digital or print."),
            Column("spent_on", "2026-09-01", "When.", True),
            Column("amount", "850000.00", "How much.", True),
        ),
        rows=marketing_spend_rows,
    ),
}

ORDER = ("inventory", "demand", "receivables", "service", "machines",
         "commitments", "trial_balance", "employees", "opportunities",
         "marketing_spend")


# ------------------------------------------------------------------ output


def sample_csv(dataset_id: str) -> str:
    """A populated dataset: edit these numbers and upload the file back."""
    ds = DATASETS[dataset_id]
    return _csv(ds.header, ds.rows())


def template_csv(dataset_id: str) -> str:
    """Headers plus one example row, for pasting your own export into."""
    ds = DATASETS[dataset_id]
    return _csv(ds.header, [{c.name: c.example for c in ds.columns}])


def notes_md() -> str:
    """The README that ships inside the pack."""
    lines = [
        "# S&OP Insight data pack",
        "",
        "Five CSV files. Edit the numbers, upload them back, and the dashboard",
        "recalculates from your figures. Nothing here is real data.",
        "",
        "## How to use it",
        "",
        "1. Open any file in Excel.",
        "2. Replace the rows with your own, keeping the header row exactly as it is.",
        "3. Save as CSV, not XLSX.",
        "4. Upload on the Data pack screen.",
        "",
        "## The three rules that matter",
        "",
        "- `item_key` must be identical in `inventory.csv` and `demand_history.csv`.",
        "  A mismatch means the item is reported with no history rather than",
        "  silently skipped.",
        "- `customer_key` must be the same code in every entity's file. This is the",
        "  only way group exposure to one hospital can be seen.",
        "- A zero-demand month is information. Send the row with `0`; do not omit it.",
        "",
        "## Files",
        "",
    ]
    for key in ORDER:
        ds = DATASETS[key]
        lines += [f"### {ds.filename} — {ds.title}", "", ds.description, "", "| Column | Example | Notes |", "| --- | --- | --- |"]
        for c in ds.columns:
            req = " **(required)**" if c.required else ""
            lines.append(f"| `{c.name}` | `{c.example}` | {c.note}{req} |")
        lines.append("")

    lines += [
        "## Thresholds are not in these files",
        "",
        "Cost of capital, target cover, the excess limit, DSO target, labour rate",
        "and payback target are settings, not data. Change them on the dashboard;",
        "every figure recalculates and the run records which values produced it.",
        "",
        "## Units",
        "",
        "Quantities are units of your own unit of measure, money is PKR, demand is",
        "per calendar month, and lead time is in calendar days. The engine never",
        "guesses a unit. If your export is in thousands, convert before uploading.",
    ]
    return "\n".join(lines) + "\n"


def pack(templates: bool = False) -> Dict[str, bytes]:
    """Every file in the pack, ready to be zipped."""
    out: Dict[str, bytes] = {}
    for key in ORDER:
        ds = DATASETS[key]
        body = template_csv(key) if templates else sample_csv(key)
        name = ds.filename.replace(".csv", "_template.csv") if templates else ds.filename
        out[name] = body.encode("utf-8")
    out["README.md"] = notes_md().encode("utf-8")
    return out
