"""The import workbook a customer fills in, and two filled examples.

The shape is the point. Items are stated once and stock is stated per
branch, on separate sheets, so an item's lead time cannot disagree with
itself across four rows -- which is what a single flat sheet invites and
what nobody notices until a forecast comes out wrong.

The two examples are deliberately different businesses. One sells
reagents and consumables: short lead times, steady demand, low unit
cost. The other sells capital equipment and spares: lead times in
months, demand that arrives in ones and twos, unit costs a hundred
times higher. A demo built from one of them teaches you nothing about
the other, and the engine's classification has nothing to classify.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

MONTHS = 24
BRANCH_COUNT = 4

ARIAL = "Arial"
HEAD = Font(name=ARIAL, bold=True, color="FFFFFF", size=10)
BODY = Font(name=ARIAL, size=10)
INPUT = Font(name=ARIAL, size=10, color="0000FF")      # you type it
CALC = Font(name=ARIAL, size=10)                        # formula
NOTE = Font(name=ARIAL, size=9, italic=True, color="595959")
TITLE = Font(name=ARIAL, bold=True, size=13)
H2 = Font(name=ARIAL, bold=True, size=10)

NAVY = PatternFill("solid", fgColor="1F3864")
GREY = PatternFill("solid", fgColor="F2F2F2")
YELLOW = PatternFill("solid", fgColor="FFF2CC")
RULE = Border(bottom=Side(style="thin", color="D9D9D9"))

MONEY = '#,##0;(#,##0);-'
QTY = '#,##0;(#,##0);-'
DEC = '#,##0.0;(#,##0.0);-'


# --------------------------------------------------------------- the data

ORGS = {
    "meridian": {
        "name": "Meridian Diagnostics",
        "what": "Reagents and consumables for hospital laboratories",
        "currency": "PKR",
        "branches": [
            ("KHI", "Karachi", "Karachi", True),
            ("LHR", "Lahore", "Lahore", True),
            ("ISB", "Islamabad", "Islamabad", True),
            ("FSD", "Faisalabad", "Faisalabad", False),
        ],
        "suppliers": [
            ("Sysmex Asia Pacific", "Singapore", "USD"),
            ("Roche Diagnostics", "Germany", "EUR"),
            ("Mindray Bio-Medical", "China", "USD"),
            ("Erba Lachema", "Czechia", "EUR"),
        ],
        "lead": (21, 60),
        "cost": (650, 16_000),
        "families": [
            ("Haematology reagent pack", "Reagents"),
            ("Chemistry calibrator", "Reagents"),
            ("Blood gas cartridge", "Consumables"),
            ("Urine strip bottle", "Consumables"),
            ("Control serum level", "Reagents"),
            ("Cuvette tray", "Consumables"),
            ("ESR tube pack", "Consumables"),
            ("Wash solution", "Reagents"),
            ("Pipette tip rack", "Consumables"),
            ("Coagulation reagent", "Reagents"),
            ("Electrolyte electrode", "Spares"),
            ("Lancet box", "Consumables"),
        ],
        "pattern_mix": ["smooth", "smooth", "seasonal", "trending",
                        "erratic", "lumpy", "intermittent"],
        "customers": [
            ("Civil Hospital Karachi", "Karachi", True, False),
            ("Aga Khan University Hospital", "Karachi", False, False),
            ("Shifa International", "Islamabad", False, False),
            ("Indus Hospital", "Karachi", False, False),
            ("Services Hospital Lahore", "Lahore", True, False),
            ("Meridian Supplies", "Karachi", False, True),
        ],
        "invoice": (180_000, 4_800_000),
        # The figures the money sheets are built from. They are a
        # business's shape, not decoration: a reagent house turns stock
        # over quickly, carries little plant and finances the gap
        # between a letter of credit and a public hospital paying.
        "staff": [
            ("Imran Shah", "sales", 185_000),
            ("Sana Raza", "service", 145_000),
            ("Bilal Khan", "warehouse", 62_000),
            ("Hina Qureshi", "finance", 160_000),
            ("Adeel Mirza", "sales", 140_000),
            ("Nadia Aslam", "service", 120_000),
            ("Tariq Javed", "warehouse", 58_000),
        ],
        "contacts": ["Dr Farooq Ahmed", "Ms Rabia Sattar",
                     "Dr Kamran Baig", "Mr Owais Dar",
                     "Dr Shazia Malik", "Mr Junaid Rafi"],
        "rent": 320_000,
        "loan_instalment": 1_250_000,
        "bank": 3_250_000,
        "fixed_assets": 14_000_000,
        "accum_depreciation": 3_000_000,
        "depreciation": 1_250_000,
        "finance_cost": 2_050_000,
        "short_term_debt": 8_000_000,
        "new_borrowing": 1_000_000,
        "equity": 5_000_000,
        "retained_earnings": 8_200_000,
        "dividend": 1_000_000,
        "capex": 6_500_000,
        "machine_prefix": "AN",
        "machines": [
            ("Sysmex XN-550 haematology analyser", 3_200_000, 900, 180),
            ("Mindray BS-240 chemistry analyser", 2_450_000, 1400, 95),
            ("Erba XL-200 chemistry analyser", 1_900_000, 1100, 110),
            ("Sysmex CS-1600 coagulation analyser", 4_100_000, 400, 320),
            ("Mindray BC-5150 cell counter", 1_650_000, 1800, 70),
        ],
    },
    "indus": {
        "name": "Indus Medical Systems",
        "what": "Capital equipment and spare parts for hospitals",
        "currency": "PKR",
        "branches": [
            ("KHI", "Karachi", "Karachi", True),
            ("LHR", "Lahore", "Lahore", True),
            ("PEW", "Peshawar", "Peshawar", False),
            ("MUL", "Multan", "Multan", False),
        ],
        "suppliers": [
            ("GE HealthCare", "United States", "USD"),
            ("Draeger Medical", "Germany", "EUR"),
            ("Fresenius Kabi", "Germany", "EUR"),
            ("Contec Medical", "China", "USD"),
        ],
        "lead": (60, 150),
        "cost": (7_500, 420_000),
        "families": [
            ("Ultrasound probe", "Probes"),
            ("Patient monitor module", "Modules"),
            ("Defibrillator paddle set", "Spares"),
            ("Centrifuge rotor", "Spares"),
            ("Incubator temperature sensor", "Sensors"),
            ("Infusion pump battery", "Spares"),
            ("Ventilator flow sensor", "Sensors"),
            ("X-ray tube insert", "Spares"),
            ("ECG patient cable", "Cables"),
            ("Suction pump diaphragm", "Spares"),
            ("Anaesthesia vaporiser", "Modules"),
            ("Surgical light bulb", "Spares"),
        ],
        "pattern_mix": ["intermittent", "intermittent", "lumpy", "lumpy",
                        "erratic", "smooth", "trending"],
        "customers": [
            ("Punjab Institute of Cardiology", "Lahore", True, False),
            ("Liaquat National Hospital", "Karachi", False, False),
            ("Lady Reading Hospital", "Peshawar", True, False),
            ("Nishtar Hospital Multan", "Multan", True, False),
            ("Hameed Latif Hospital", "Lahore", False, False),
            ("Indus Medical Trading", "Karachi", False, True),
        ],
        "invoice": (450_000, 14_000_000),
        "staff": [
            ("Faisal Siddiqui", "sales", 260_000),
            ("Ayesha Noor", "service", 210_000),
            ("Zeeshan Haider", "service", 195_000),
            ("Mehwish Tariq", "finance", 185_000),
            ("Usman Ghani", "warehouse", 78_000),
            ("Rizwan Akhtar", "sales", 225_000),
        ],
        "contacts": ["Dr Asma Jahangir", "Mr Saad Pervez",
                     "Dr Haroon Rasheed", "Ms Fatima Zaidi",
                     "Mr Danish Iqbal", "Dr Yasir Mehmood"],
        "rent": 540_000,
        "loan_instalment": 3_400_000,
        "bank": 7_800_000,
        "fixed_assets": 46_000_000,
        "accum_depreciation": 11_500_000,
        "depreciation": 4_200_000,
        "finance_cost": 7_600_000,
        "short_term_debt": 24_000_000,
        "new_borrowing": 4_000_000,
        "equity": 15_000_000,
        "retained_earnings": 19_400_000,
        "dividend": 2_500_000,
        "capex": 18_000_000,
        "machine_prefix": "EQ",
        "machines": [
            ("GE Logiq P9 ultrasound", 7_800_000, 0, 0),
            ("Draeger Evita V300 ventilator", 5_400_000, 0, 0),
            ("Fresenius 4008S dialysis machine", 3_900_000, 0, 0),
            ("GE Carescape B650 monitor", 2_100_000, 0, 0),
        ],
    },
}

VARIANTS = ["A", "B", "C", "D", "E"]


def month_labels(end: date, n: int) -> list[str]:
    out, y, m = [], end.year, end.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def demand_series(rng: random.Random, pattern: str, base: float,
                  n: int) -> list[int]:
    """A pattern the engine can tell apart from the others.

    The engine classifies on two numbers: how often demand happens (ADI,
    cut at 1.32) and how much the non-zero sizes vary (CV-squared, cut
    at 0.49). Four quadrants, four names. A first version of this had a
    'lumpy' pattern that was infrequent but consistently sized, so its
    CV-squared came out near 0.02 and every one of them classified as
    intermittent. Variety on the generating side is not variety on the
    screen unless it moves the numbers the classifier actually reads.

    So each pattern here is built to land in its quadrant:
      smooth       frequent, steady
      seasonal     frequent, steady, with a winter lift
      trending     frequent, steady, growing
      erratic      frequent, wildly different sizes
      intermittent occasional, steady when it happens
      lumpy        occasional, and wildly different sizes when it does
    """
    out = []
    for i in range(n):
        if pattern == "smooth":
            q = rng.gauss(base, base * 0.18)
        elif pattern == "seasonal":
            # Respiratory and infection work peaks over winter here.
            month = (i % 12) + 1
            lift = 1.6 if month in (11, 12, 1, 2) else 0.85
            q = rng.gauss(base * lift, base * 0.2)
        elif pattern == "trending":
            q = rng.gauss(base * (0.6 + 0.9 * i / n), base * 0.2)
        elif pattern == "erratic":
            # Every month, but a tender one month and a trickle the next.
            q = base * rng.choice([0.15, 0.3, 0.6, 1.0, 2.5, 5.0])
        elif pattern == "lumpy":
            q = (base * rng.choice([0.5, 1.0, 4.0, 9.0, 16.0])
                 if rng.random() < 0.3 else 0)
        elif pattern == "intermittent":
            q = rng.gauss(base, base * 0.25) if rng.random() < 0.35 else 0
        else:
            q = base
        out.append(max(0, int(round(q))))
    return out


def blank() -> Workbook:
    """The same twenty-four sheets with no rows in them.

    The template somebody downloads before they have anything to
    upload. Headers, the required markers and the notes under each
    sheet, and nothing else -- so the first thing they do is type
    their own figures rather than delete somebody else's.

    Built by the same `render` as the sample, from empty lists, which
    is the point: a template assembled separately would drift from the
    importer the moment a column changed, and that is exactly what
    happened to the one this replaces.
    """
    return render(
        spec={"name": "Landed import template",
              "what": "Every sheet Landed reads, with no rows in it. Fill in "
                      "what you have and upload it. Nothing is written until "
                      "you have seen what it will do."},
        branches=[], suppliers=[], items=[], stock=[], history=[],
        orders=[], shipments=[], months=month_labels(date.today(), MONTHS),
        customers=[], invoices=[], machines=[], jobs=[],
        extra={
            "receipts": [], "fitment": [], "accounts": [], "dividends": [],
            "period": (date.today().replace(month=1, day=1).isoformat(),
                       date.today().isoformat()),
            "commitments": [], "bank": [], "employees": [], "pipeline": [],
            "spend": [], "contacts": [], "dealings": [],
        },
    )


def build(key: str, seed: int) -> Workbook:
    spec = ORGS[key]
    rng = random.Random(seed)
    today = date.today()
    months = month_labels(today, MONTHS)
    branches = spec["branches"]
    suppliers = spec["suppliers"]

    # ---- items
    items = []
    for family, category in spec["families"]:
        for v in VARIANTS:
            sku = f"{family[:3].upper()}-{rng.randint(1000, 9999)}{v}"
            lead = rng.randint(*spec["lead"])
            items.append({
                "sku": sku,
                "description": f"{family} {v}",
                "category": category,
                "supplier": rng.choice(suppliers)[0],
                "uom": "each" if category != "Reagents" else "pack",
                "lead": lead,
                "lead_std": max(1, int(lead * rng.uniform(0.08, 0.3))),
                "critical": "yes" if rng.random() < 0.22 else "",
                "cost": round(rng.uniform(*spec["cost"]), -1),
                # A selling price on most lines but not all, because a
                # real catalogue has gaps and the product should be
                # seen coping with them rather than only with a
                # complete one. Consumables carry a fatter markup than
                # instruments, which is the shape of this trade.
                "price": None if rng.random() < 0.15 else round(
                    rng.uniform(*spec["cost"])
                    * (rng.uniform(1.45, 2.3) if category == "Reagents"
                       else rng.uniform(1.18, 1.6)), -1),
                "generic": "yes" if category in ("Consumables",
                                                 "Disposables") else "",
                "pattern": rng.choice(spec["pattern_mix"]),
                "base": rng.uniform(2, 40) if key == "meridian"
                        else rng.uniform(0.4, 4),
            })
    items = items[:60]

    # ---- which items are carried where, and the stock and history
    stock, history = [], []
    for it in items:
        # Not every line is at every branch. A business that carries
        # everything everywhere is not a business anyone recognises.
        here = [b for b in branches if rng.random() < 0.78] or [branches[0]]
        for code, _name, _city, _wh in here:
            weight = 1.0 if code in ("KHI", "LHR") else rng.uniform(0.3, 0.6)
            series = demand_series(rng, it["pattern"], it["base"] * weight,
                                   MONTHS)
            recent = sum(series[-3:]) / 3 or 0.5
            # Cover as a multiple of lead time, not as a count of months.
            #
            # Absolute months is the wrong yardstick and the capital
            # business proved it: with lead times of two to five months,
            # three months of cover is genuinely below the reorder point,
            # so 78% of its lines came out needing action while the
            # reagent business looked healthy on the same numbers. Stock
            # is held against the wait, so the wait is what it scales to.
            lead_months = max(0.5, it["lead"] / 30.0)
            # Centred above 1.0, because a business that is doing its job
            # stocks to its order-up-to level, which sits above the
            # reorder point by design. Centred on 1.0 modelled a company
            # permanently on the edge of stocking out, and the screen
            # read accordingly.
            multiple = rng.choices(
                [0.3, 0.7, 1.1, 1.5, 1.8, 2.1, 2.5, 3.0, 4.0],
                weights=[3, 6, 11, 17, 20, 17, 12, 9, 5])[0]
            cover = lead_months * multiple

            # Not capped, deliberately.
            #
            # Capping it made the demo look better and hid a real defect.
            # The engine's excess cutoff is a flat six months of cover
            # while its reorder point is lead-time-relative, so for a
            # spare with a five-month lead the reorder point can sit at
            # twenty months of cover -- past the excess line. Those items
            # have no quantity at all that counts as healthy: too little
            # is "reorder", enough is "standing still", and there is
            # nothing in between. 83% of the capital lines are in that
            # state. Honest data shows it; a cap would have buried it.
            qty = int(round(recent * cover))
            expiry = ""
            if it["category"] in ("Reagents", "Consumables") and rng.random() < 0.5:
                expiry = (today + timedelta(days=rng.randint(40, 700))
                          ).isoformat()
            stock.append({
                "sku": it["sku"], "branch": code, "qty": qty,
                "cost": round(it["cost"] * rng.uniform(0.92, 1.08), -1),
                "expiry": expiry,
            })
            history.append({"sku": it["sku"], "branch": code,
                            "series": series})

    # ---- open purchase orders
    orders = []
    for n in range(14):
        it = rng.choice(items)
        code = rng.choice(branches)[0]
        sup = next((s for s in suppliers if s[0] == it["supplier"]),
                   suppliers[0])
        ordered = today - timedelta(days=rng.randint(5, 70))
        # The price is in the supplier's currency, which means dividing
        # the rupee cost by the rate rather than carrying the same
        # number under a different label.
        #
        # This generator did carry the same number: a supplier invoiced
        # 10,120 USD for an item worth 10,120 PKR. Nothing multiplied
        # price by rate, so it never showed -- until migration 0013
        # made the ledger do exactly that, at which point an imported
        # workbook would have valued stock 282 times too high. Found
        # by the first receipt that went through the whole chain.
        fx = {"USD": 282.0, "EUR": 305.0}.get(sup[2], 1.0)
        orders.append({
            "po": f"PO-{today.year}-{1000+n}",
            "supplier": sup[0], "branch": code, "sku": it["sku"],
            "qty": max(1, int(it["base"] * rng.uniform(3, 10))),
            "price": round(it["cost"] * rng.uniform(0.8, 0.95) / fx, 2),
            "currency": sup[2],
            "fx": fx,
            "ordered": ordered.isoformat(),
            "expected": (ordered + timedelta(days=it["lead"])).isoformat(),
            "status": rng.choice(["placed", "placed", "part_received"]),
        })

    # ---- shipments, their costs, and what was received off them
    #
    # Two of the four have landed and two are still at sea, because
    # both states have to be visible: an arrived shipment is where
    # landed cost becomes a real unit cost, and one in transit is
    # money that is committed and not yet on a shelf.
    shipments, receipts = [], []
    for n in range(4):
        order = orders[n]
        po = order["po"]
        ref = f"SHP-{today.year}-{200+n}"
        arrived = n < 2
        etd = today - timedelta(days=rng.randint(40, 70) if arrived
                                else rng.randint(2, 20))
        eta = etd + timedelta(days=rng.randint(18, 34))
        # The ocean freight is billed in dollars, because it usually
        # is: the shipping line is foreign and the clearing agent and
        # the customs duty are not. A workbook where every charge
        # happens to be in rupees would never exercise the conversion.
        for cost_type, basis, amount, cur in (
            ("Ocean freight", "value",
             round(rng.randint(80_000, 260_000) / 282.0, 2), "USD"),
            ("Customs duty", "value", rng.randint(120_000, 420_000), "PKR"),
            ("Clearing agent", "qty", rng.randint(18_000, 45_000), "PKR"),
            ("Inland transport", "weight",
             rng.randint(12_000, 38_000), "PKR"),
        ):
            shipments.append({
                "ref": ref, "po": po, "mode": rng.choice(["sea", "air"]),
                "etd": etd.isoformat(), "eta": eta.isoformat(),
                "cost_type": cost_type, "amount": amount,
                "currency": cur, "fx": 282.0 if cur == "USD" else 1.0,
                "allocate_by": basis,
            })
        if not arrived:
            continue
        # Received the day it cleared, which is the day the cost
        # becomes a cost. Weight and volume are per line because two
        # of the four charges are spread on them.
        qty = order["qty"]
        receipts.append({
            "grn": f"GRN-{today.year}-{700+n}",
            "shipment": ref,
            "po": po,
            "branch": order["branch"],
            "sku": order["sku"],
            "qty": qty,
            "price": order["price"],
            "currency": order["currency"],
            "fx": order["fx"],
            "received": (eta + timedelta(days=rng.randint(1, 4))).isoformat(),
            "weight": round(qty * rng.uniform(0.4, 2.2), 2),
            "volume": round(qty * rng.uniform(0.002, 0.012), 4),
            "batch": f"B{rng.randint(1000, 9999)}",
            "expiry": (today + timedelta(
                days=rng.randint(240, 900))).isoformat(),
        })

    # ---- customers, and what they owe
    customers, invoices = [], []
    for n, (name, city, public, inter) in enumerate(spec["customers"]):
        code = "".join(w[0] for w in name.split()[:3]).upper() + f"{n:02d}"
        customers.append({
            "code": code, "name": name, "city": city, "public": public,
            "inter": inter, "terms": 60 if public else 30,
            "contact": "", "email": "",
        })
    for n in range(12):
        c = rng.choice(customers)
        issued = today - timedelta(days=rng.randint(5, 210))
        amount = round(rng.uniform(*spec["invoice"]), -3)
        # Most invoices are settled; the open book is the minority, which
        # is what makes the overdue ones worth looking at.
        paid = amount if rng.random() < 0.45 else (
            round(amount * rng.choice([0, 0, 0.25, 0.5]), -3))
        invoices.append({
            "no": f"INV-{today.year}-{140+n}", "customer": c["code"],
            "branch": rng.choice(branches)[0],
            "date": issued.isoformat(),
            "due": (issued + timedelta(days=c["terms"])).isoformat(),
            "amount": amount, "paid": paid,
        })

    # ---- machines placed, and the jobs done on them
    machines, jobs = [], []
    for n, (model, capital, tests, per_test) in enumerate(spec["machines"]):
        c = rng.choice(customers)
        placed = today - timedelta(days=rng.randint(120, 900))
        machines.append({
            "key": f"{spec['machine_prefix']}-{1000+n}", "model": model,
            "serial": f"{rng.randint(10**7, 10**8-1)}",
            "customer": c["code"], "branch": rng.choice(branches)[0],
            "placed": placed.isoformat(), "capital": capital,
            "months": rng.choice([24, 36, 36, 60]),
            "tests": tests, "per_test": per_test,
            "margin": round(rng.uniform(0.3, 0.52), 2),
        })
    for n in range(18):
        m = rng.choice(machines) if machines else None
        opened = today - timedelta(days=rng.randint(3, 330))
        hours = round(rng.uniform(1, 9), 1)
        # Not every hour gets invoiced, and that gap is the finding.
        billed = round(hours * rng.choice([0, 0.3, 0.5, 1.0, 1.0]), 1)
        parts = round(rng.uniform(0, 90_000), -3)
        warranty = rng.random() < 0.25
        jobs.append({
            "no": f"JOB-{today.year}-{300+n}",
            "machine": m["key"] if m else "",
            "branch": rng.choice(branches)[0],
            "opened": opened.isoformat(),
            "closed": (opened + timedelta(days=rng.randint(0, 6))).isoformat()
                      if rng.random() < 0.8 else "",
            "hours": hours, "billed": billed,
            "parts": parts,
            "charged": 0 if warranty else round(
                parts * rng.choice([0, 0.5, 1.0, 1.0]), -3),
            "warranty": warranty, "repeat": rng.random() < 0.2,
            "down": round(rng.uniform(0, 4), 1),
        })

    # ---- the people on the payroll
    #
    # Pay is split into basic, allowances and medical because the
    # deductions are: tax is on the taxable part, EOBI is charged on the
    # minimum wage rather than on pay, and medical is exempt up to a
    # tenth of basic. A single "salary" column cannot produce a correct
    # payslip, so the sheet does not offer one.
    employees = []
    for n, (name, dept, basic) in enumerate(spec["staff"]):
        employees.append({
            "no": f"E-{100+n}",
            "name": name,
            "dept": dept,
            "branch": branches[n % len(branches)][0],
            "basic": basic,
            "allow": round(basic * 0.30, -2),
            "medical": round(basic * 0.08, -2),
            "pf_er": 0.05,
            "pf_ee": 0.05,
            "joined": (today - timedelta(days=rng.randint(200, 2600)))
                      .isoformat(),
        })

    # ---- money already promised
    #
    # Without these the thirteen-week view is fiction: the letter of
    # credit settles and the salaries go out whatever the stock position
    # says. The recurring ones are held once and spread by the engine,
    # so changing the rent changes every week at once.
    payroll_monthly = sum(e["basic"] + e["allow"] + e["medical"]
                          for e in employees)
    commitments = [
        {"label": "Salaries", "kind": "payroll", "recurring": "yes",
         "every": 30, "amount": round(payroll_monthly, -3),
         "due": (today + timedelta(days=rng.randint(2, 20))).isoformat(),
         "branch": branches[0][0]},
        {"label": "Warehouse rent", "kind": "rent", "recurring": "yes",
         "every": 30, "amount": spec["rent"],
         "due": (today + timedelta(days=rng.randint(1, 28))).isoformat(),
         "branch": branches[0][0]},
        {"label": "Bank facility repayment", "kind": "loan_repayment",
         "recurring": "yes", "every": 90,
         "amount": spec["loan_instalment"],
         "due": (today + timedelta(days=rng.randint(10, 60))).isoformat(),
         "branch": branches[0][0]},
    ]
    # One letter of credit per open order, at close to full value, due
    # about a week after the order: in this market the money leaves when
    # the LC is opened, not when the goods arrive.
    for o in orders[:4]:
        commitments.append({
            "label": f"LC {o['po']}",
            "kind": "lc_settlement", "recurring": "", "every": "",
            "amount": round(o["qty"] * o["price"] * o["fx"], -3),
            "due": (today + timedelta(days=rng.randint(3, 40))).isoformat(),
            "branch": o["branch"],
        })

    bank = [{
        "as_at": (today - timedelta(days=1)).isoformat(),
        "balance": spec["bank"],
        "account": "Main account",
    }]

    # ---- the pipeline
    #
    # Each row names the item it would consume, which is what lets the
    # engine check a tender against the shelf: one closing in six weeks
    # for a line with eleven weeks of lead time is a disappointment with
    # a date on it.
    STAGES = [("lead", 40), ("qualified", 25), ("quoted", 35),
              ("tender_submitted", 30), ("won", 25), ("lost", 20)]
    pipeline = []
    for n in range(14):
        c = rng.choice(customers)
        it = rng.choice(items)
        stage = rng.choices([s for s, _ in STAGES],
                            weights=[w for _, w in STAGES])[0]
        closed = stage in ("won", "lost")
        opened = today - timedelta(days=rng.randint(30, 300))
        pipeline.append({
            "ref": f"TND-{today.year}-{500+n}",
            "customer": c["code"],
            "stage": stage,
            "value": round(rng.uniform(*spec["invoice"]) * 3, -3),
            "close": (today + timedelta(days=rng.randint(-120, 150)))
                     .isoformat(),
            "sku": it["sku"],
            "qty": rng.randint(10, 400),
            "source": rng.choice(["tender", "referral", "conference",
                                  "cold"]),
            "owner": rng.choice(spec["staff"])[0],
            "opened": opened.isoformat(),
            "closed": (opened + timedelta(days=rng.randint(20, 120))
                       ).isoformat() if closed else "",
        })

    spend = [
        {"label": "Pathology conference stand", "category": "conference",
         "on": (today - timedelta(days=70)).isoformat(), "amount": 450_000},
        {"label": "Demonstration unit", "category": "demo_unit",
         "on": (today - timedelta(days=130)).isoformat(), "amount": 900_000},
        {"label": "Sample kits to tender accounts", "category": "samples",
         "on": (today - timedelta(days=40)).isoformat(), "amount": 180_000},
        {"label": "Trade directory listing", "category": "print",
         "on": (today - timedelta(days=200)).isoformat(), "amount": 60_000},
    ]

    # ---- who to ring, and what was last said
    contacts, dealings = [], []
    for n, c in enumerate(customers):
        contacts.append({
            "customer": c["code"], "supplier": "",
            "name": spec["contacts"][n % len(spec["contacts"])],
            "role": rng.choice(["Head of Pathology", "Procurement officer",
                                "Lab manager", "Biomedical engineer"]),
            "email": "", "phone": "", "whatsapp": "",
            "primary": "yes" if n % 2 == 0 else "",
        })
    for n in range(8):
        c = rng.choice(customers)
        when = today - timedelta(days=rng.randint(1, 90))
        dealings.append({
            "customer": c["code"],
            "on": when.isoformat(),
            "channel": rng.choice(["call", "visit", "email", "whatsapp",
                                   "meeting"]),
            "summary": rng.choice([
                "Chased the overdue invoices; finance said next cycle.",
                "Walked through the tender specification.",
                "Complaint about a late delivery; apologised and reshipped.",
                "Asked about a second analyser on placement.",
                "Quarterly review; volumes steady.",
            ]),
            "next": rng.choice(["Send a revised quote", "Call back", ""]),
            "next_on": (when + timedelta(days=rng.randint(7, 30))
                        ).isoformat() if rng.random() < 0.6 else "",
        })

    # ---- the trial balance
    #
    # Derived, not invented, and that is the point. Three checks run on
    # these figures: the sheet balances at both ends, reserves move by
    # profit less the dividend, and the cash flow ties. A set of
    # plausible round numbers fails all three, and then the example
    # teaches somebody that the checks are noise.
    #
    # Two things make it tie. Every closing figure that exists elsewhere
    # in this workbook is taken from there -- stock at cost, the open
    # receivables, the bank balance -- and creditors are the plug. And
    # accumulated depreciation moves by exactly the depreciation charge,
    # with no disposals, which is what makes the indirect cash flow come
    # out at the movement in cash.
    stock_value = round(sum(s["qty"] * s["cost"] for s in stock), -3)
    open_recv = round(sum(i["amount"] - i["paid"] for i in invoices), -3)

    revenue = round(sum(i["amount"] for i in invoices) * 4.0, -3)
    cogs = round(revenue * 0.64, -3)
    payroll_year = round(payroll_monthly * 12, -3)
    opex = round(revenue * 0.07, -3)
    depreciation = spec["depreciation"]
    finance_cost = spec["finance_cost"]
    tax = round(max(0.0, revenue - cogs - payroll_year - opex
                    - depreciation - finance_cost) * 0.29, -3)
    pat = round(revenue - cogs - payroll_year - opex - depreciation
                - finance_cost - tax, 2)
    dividend = spec["dividend"]
    capex = spec["capex"]

    o_cash, o_recv = spec["bank"] * 0.7, open_recv * 0.85
    o_inv, o_fixed = stock_value * 0.9, spec["fixed_assets"]
    o_accdep = spec["accum_depreciation"]
    o_std, o_taxpay, o_equity = spec["short_term_debt"], tax * 0.6, spec["equity"]
    o_re = spec["retained_earnings"]
    o_assets = o_cash + o_recv + o_inv + o_fixed - o_accdep
    o_payables = o_assets - o_std - o_taxpay - o_equity - o_re

    c_cash, c_recv, c_inv = spec["bank"], open_recv, stock_value
    c_fixed, c_accdep = o_fixed + capex, o_accdep + depreciation
    c_std, c_taxpay = o_std + spec["new_borrowing"], tax * 0.8
    c_re = o_re + pat - dividend
    c_assets = c_cash + c_recv + c_inv + c_fixed - c_accdep
    c_payables = c_assets - c_std - c_taxpay - o_equity - c_re

    year_start = date(today.year, 1, 1)
    accounts = [
        ("4000", "Sales", "revenue", 0, revenue),
        ("5000", "Cost of sales", "cogs", 0, cogs),
        ("6000", "Salaries and wages", "payroll", 0, payroll_year),
        ("6100", "Other overheads", "opex", 0, opex),
        ("6200", "Depreciation", "depreciation", 0, depreciation),
        ("7000", "Bank mark-up", "finance_cost", 0, finance_cost),
        ("8000", "Taxation", "tax", 0, tax),
        ("1000", "Bank", "cash", o_cash, c_cash),
        ("1100", "Trade debtors", "receivables", o_recv, c_recv),
        ("1200", "Stock in trade", "inventory", o_inv, c_inv),
        ("1500", "Plant and equipment", "fixed_assets", o_fixed, c_fixed),
        ("1510", "Depreciation to date", "accumulated_depreciation",
         o_accdep, c_accdep),
        ("2000", "Trade creditors", "payables", o_payables, c_payables),
        ("2100", "Running finance", "short_term_debt", o_std, c_std),
        ("2200", "Taxation payable", "tax_payable", o_taxpay, c_taxpay),
        ("3000", "Share capital", "equity", o_equity, o_equity),
        ("3100", "Retained earnings", "retained_earnings", o_re, c_re),
    ]
    dividends = [{
        "kind": "dividend",
        "on": date(today.year, 6, 30).isoformat(),
        "amount": dividend,
        "note": "Interim dividend",
    }] if dividend else []

    # ---- which reagent runs on which analyser
    #
    # Only reagents, and only onto machines, because that is the real
    # relationship: a box of gloves fits everything and is marked
    # generic instead. Some reagents fit two analysers, which is the
    # case a column on the item could not have represented.
    fitment = []
    reagents = [i for i in items if i["category"] == "Reagents"]
    for it in reagents:
        if not machines or rng.random() < 0.2:
            continue
        for m in rng.sample(machines, k=min(len(machines),
                                            1 if rng.random() < 0.7 else 2)):
            fitment.append({"sku": it["sku"], "machine": m["key"]})

    extra = {
        "receipts": receipts,
        "fitment": fitment,
        "accounts": accounts, "dividends": dividends,
        "period": (year_start.isoformat(), today.isoformat()),
        "commitments": commitments, "bank": bank,
        "employees": employees, "pipeline": pipeline, "spend": spend,
        "contacts": contacts, "dealings": dealings,
    }

    return render(spec, branches, suppliers, items, stock, history,
                  orders, shipments, months, customers, invoices,
                  machines, jobs, extra)


# --------------------------------------------------------------- the sheets

def head(ws, headers, widths, required=()):
    for i, h in enumerate(headers, 1):
        c = ws.cell(1, i, h)
        c.font = HEAD
        c.fill = NAVY
        c.alignment = Alignment(vertical="center", wrap_text=True)
        if h in required:
            c.value = f"{h} *"
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = "A2"


def put(ws, r, values, fonts=None, formats=None):
    for i, v in enumerate(values, 1):
        c = ws.cell(r, i, v)
        c.font = (fonts or {}).get(i, INPUT)
        c.border = RULE
        if formats and i in formats:
            c.number_format = formats[i]


def render(spec, branches, suppliers, items, stock, history, orders,
           shipments, months, customers, invoices, machines, jobs,
           extra):
    wb = Workbook()
    del wb["Sheet"]

    # ------------------------------------------------------------ Read me
    ws = wb.create_sheet("Read me")
    ws.column_dimensions["A"].width = 112
    lines = [
        (spec["name"], TITLE),
        (spec["what"], NOTE),
        ("", BODY),
        ("What this is", H2),
        ("Every sheet Landed can read. Fill in what you have, upload "
         "the file, and the figures in", BODY),
        ("the app are yours. The example rows are invented -- replace "
         "them with your own.", BODY),
        ("", BODY),
        ("Nothing here is all-or-nothing. Each sheet switches on the "
         "part of the app that reads it,", BODY),
        ("so four sheets gets you stock and reordering, and the rest "
         "can wait until you have them.", BODY),
        ("", BODY),
        ("Start here", H2),
        ("Branches             every branch and warehouse you hold "
         "stock at.", BODY),
        ("Items                every line you carry. Once each, not "
         "once per branch.", BODY),
        ("Stock                what is on the shelf today, per item per "
         "branch. Your count BEFORE", BODY),
        ("                     anything on Goods received.", BODY),
        ("Sales history        what you sold each month. Months run "
         "across the top.", BODY),
        ("", BODY),
        ("Goods", H2),
        ("On order             purchase orders placed and not yet "
         "arrived.", BODY),
        ("Shipments            what is in transit, and the freight and "
         "duty on it.", BODY),
        ("Goods received       what actually arrived off those "
         "shipments. This is the sheet that", BODY),
        ("                     turns freight and duty into a cost per "
         "unit -- without it the costs", BODY),
        ("                     have no goods to spread across, and "
         "every price falls back to the", BODY),
        ("                     supplier's invoice.", BODY),
        ("Fitment              which analyser each reagent runs on.", BODY),
        ("Suppliers            who you buy from. Skipped, they are "
         "created from the names on Items.", BODY),
        ("", BODY),
        ("Money", H2),
        ("Customers            who you sell to, their terms and their "
         "credit limit.", BODY),
        ("Invoices             what you have billed and what has been "
         "paid.", BODY),
        ("Bank balance         what the cash weeks open from.", BODY),
        ("Committed payments   money already promised: letters of "
         "credit, salaries, rent, loans.", BODY),
        ("Trial balance        your accounts, for the profit, balance "
         "sheet and cash flow.", BODY),
        ("Dividends            only if you paid one.", BODY),
        ("", BODY),
        ("Service", H2),
        ("Machines             instruments placed with a customer, and "
         "what they cost you.", BODY),
        ("Service jobs         hours and parts that went in, against "
         "what was billed.", BODY),
        ("", BODY),
        ("People and selling", H2),
        ("Employees            for payroll, at the rates you set in "
         "Settings.", BODY),
        ("Contacts             who to ring at each customer and "
         "supplier.", BODY),
        ("Dealings             what was last said, and what happens "
         "next.", BODY),
        ("Pipeline             tenders and accounts being worked.", BODY),
        ("Marketing spend      what winning them costs.", BODY),
        ("", BODY),
        ("Three rules worth knowing", H2),
        ("A blank month on Sales history means you sold none that "
         "month. That is a fact and the", BODY),
        ("forecast uses it. If you genuinely have no record for a "
         "month, delete the whole column", BODY),
        ("instead -- a zero and a gap mean different things, and only "
         "one of them is true.", BODY),
        ("", BODY),
        ("An item's lead time, supplier and description live on Items "
         "and nowhere else. That is why", BODY),
        ("Items and Stock are separate sheets: stated once, they cannot "
         "disagree with themselves.", BODY),
        ("", BODY),
        ("Importing the same file twice is safe. Before it writes "
         "anything, the app compares every", BODY),
        ("row against what you already have and tells you how many are "
         "new, how many are changed", BODY),
        ("and how many are already there.", BODY),
        ("", BODY),
        ("A column marked * is required. Blue text is something you "
         "type.", NOTE),
        ("Check the Check sheet before you upload. It counts the "
         "problems that stop an import.", NOTE),
    ]
    for r, (text, font) in enumerate(lines, 1):
        c = ws.cell(r, 1, text)
        c.font = font

    # ----------------------------------------------------------- Branches
    ws = wb.create_sheet("Branches")
    head(ws, ["branch_code", "branch_name", "city", "is_warehouse"],
         [16, 24, 18, 14], required=("branch_code", "branch_name"))
    for r, (code, name, city, wh) in enumerate(branches, 2):
        put(ws, r, [code, name, city, "yes" if wh else ""])

    # -------------------------------------------------------------- Items
    ws = wb.create_sheet("Items")
    head(ws, ["sku", "description", "category", "supplier_name", "uom",
              "lead_time_days", "lead_time_days_std",
              "is_clinical_critical", "list_price", "is_generic"],
         [16, 34, 16, 24, 10, 15, 18, 18, 14, 12],
         required=("sku", "description", "lead_time_days"))
    for r, it in enumerate(items, 2):
        put(ws, r, [it["sku"], it["description"], it["category"],
                    it["supplier"], it["uom"], it["lead"], it["lead_std"],
                    it["critical"], it.get("price"), it.get("generic")],
            formats={6: QTY, 7: QTY, 9: MONEY})
    ws.cell(len(items) + 3, 1,
            "lead_time_days is order to shelf. lead_time_days_std is how "
            "much that varies -- blank means steady.").font = NOTE
    ws.cell(len(items) + 4, 1,
            "list_price is what you sell it for. Against the landed cost "
            "it gives the margin on the line, which is not the").font = NOTE
    ws.cell(len(items) + 5, 1,
            "same as the margin on the invoice. is_generic means it fits "
            "anything -- gloves, tips -- so an empty Fitment").font = NOTE
    ws.cell(len(items) + 6, 1,
            "row for it is a fact rather than something nobody has "
            "filled in yet.").font = NOTE

    # -------------------------------------------------------------- Stock
    ws = wb.create_sheet("Stock")
    head(ws, ["sku", "branch_code", "qty_on_hand", "unit_cost",
              "value", "expiry_date"],
         [16, 14, 14, 14, 16, 14],
         required=("sku", "branch_code", "qty_on_hand"))
    for r, s in enumerate(stock, 2):
        put(ws, r, [s["sku"], s["branch"], s["qty"], s["cost"], None,
                    s["expiry"]],
            formats={3: QTY, 4: MONEY, 5: MONEY})
        # A formula, not a number: change the count or the cost and the
        # value follows, which is the whole reason to open it in Excel.
        c = ws.cell(r, 5, f"=C{r}*D{r}")
        c.font = CALC
        c.number_format = MONEY
        c.border = RULE
    n = len(stock) + 1
    t = ws.cell(n + 2, 4, "Total at cost")
    t.font = H2
    v = ws.cell(n + 2, 5, f"=SUM(E2:E{n})")
    v.font = H2
    v.number_format = MONEY
    ws.cell(n + 4, 1, "unit_cost is landed cost per unit if you know it, "
                      "otherwise what the supplier charged.").font = NOTE

    # ------------------------------------------------------ Sales history
    ws = wb.create_sheet("Sales history")
    cols = ["sku", "branch_code"] + [f"sold_{m.replace('-', '_')}"
                                     for m in months] + ["total", "monthly avg"]
    head(ws, cols, [16, 14] + [11] * MONTHS + [12, 13],
         required=("sku", "branch_code"))
    for r, h in enumerate(history, 2):
        put(ws, r, [h["sku"], h["branch"]] + h["series"],
            formats={i: QTY for i in range(3, 3 + MONTHS)})
        last = get_column_letter(2 + MONTHS)
        for col, formula, fmt in (
            (3 + MONTHS, f"=SUM(C{r}:{last}{r})", QTY),
            (4 + MONTHS, f"=IFERROR(AVERAGE(C{r}:{last}{r}),0)", DEC),
        ):
            c = ws.cell(r, col, formula)
            c.font = CALC
            c.number_format = fmt
            c.border = RULE
    ws.cell(len(history) + 3, 1,
            "A blank month means you sold none. If you have no record for "
            "a month, delete that column instead.").font = NOTE

    # ----------------------------------------------------------- On order
    ws = wb.create_sheet("On order")
    head(ws, ["po_number", "supplier_name", "branch_code", "sku", "qty",
              "unit_price", "currency", "fx_rate", "ordered_at",
              "expected_at", "status"],
         [16, 24, 13, 16, 10, 14, 10, 11, 13, 13, 15],
         required=("po_number", "branch_code", "sku", "qty"))
    for r, o in enumerate(orders, 2):
        put(ws, r, [o["po"], o["supplier"], o["branch"], o["sku"], o["qty"],
                    o["price"], o["currency"], o["fx"], o["ordered"],
                    o["expected"], o["status"]],
            formats={5: QTY, 6: MONEY, 8: '#,##0.00'})
    ws.cell(len(orders) + 3, 1,
            "fx_rate converts unit_price to PKR. Held per order because it "
            "moves, and your margin lives in it.").font = NOTE
    ws.cell(len(orders) + 4, 1,
            "status: placed, or part_received if some of it has already "
            "arrived.").font = NOTE

    # ---------------------------------------------------------- Shipments
    ws = wb.create_sheet("Shipments")
    head(ws, ["shipment_ref", "po_number", "mode", "etd", "eta",
              "cost_type", "amount", "currency", "fx_rate", "allocate_by"],
         [16, 16, 10, 13, 13, 20, 14, 10, 11, 14],
         required=("shipment_ref", "po_number", "cost_type", "amount",
                   "allocate_by"))
    for r, s in enumerate(shipments, 2):
        put(ws, r, [s["ref"], s["po"], s["mode"], s["etd"], s["eta"],
                    s["cost_type"], s["amount"], s.get("currency", "PKR"),
                    s.get("fx", 1), s["allocate_by"]],
            formats={7: MONEY, 9: '#,##0.00'})
    ws.cell(len(shipments) + 3, 1,
            "One row per cost, not per shipment: a shipping line bills by "
            "volume and customs by value, so each").font = NOTE
    ws.cell(len(shipments) + 4, 1,
            "cost says how it is spread. allocate_by: value, weight, "
            "volume or qty.").font = NOTE

    # ----------------------------------------------------- Goods received
    #
    # The sheet that makes landed cost real.
    #
    # A shipment and its costs on their own allocate to nothing: the
    # cost of freight is spread across the goods it carried, and
    # without the goods there is no denominator. This sheet is the
    # goods. Each row is a receipt line, tied to the shipment it came
    # off, and it is what turns PKR 620,000 of freight and duty into a
    # unit cost on a shelf.
    #
    # It posts a stock movement, so the Stock sheet is the count
    # BEFORE these arrived. Listing the same goods in both counts them
    # twice.
    ws = wb.create_sheet("Goods received")
    head(ws, ["grn_number", "shipment_ref", "po_number", "branch_code",
              "sku", "qty", "unit_price", "currency", "fx_rate",
              "received_at", "weight_kg", "volume_cbm", "batch",
              "expiry_date"],
         [17, 16, 16, 13, 16, 10, 13, 10, 11, 14, 12, 13, 12, 13],
         required=("grn_number", "branch_code", "sku", "qty"))
    for r, g in enumerate(extra.get("receipts", []), 2):
        put(ws, r, [g["grn"], g["shipment"], g["po"], g["branch"], g["sku"],
                    g["qty"], g["price"], g["currency"], g["fx"],
                    g["received"], g["weight"], g["volume"], g["batch"],
                    g["expiry"]],
            formats={6: QTY, 7: MONEY, 9: '#,##0.00',
                     11: '#,##0.00', 12: '#,##0.0000'})
    _ng = len(extra.get("receipts", []))
    ws.cell(_ng + 3, 1,
            "unit_price is what the supplier charged for the goods, before "
            "freight, in the currency you were invoiced.").font = NOTE
    ws.cell(_ng + 4, 1,
            "Leave currency and fx_rate blank to take them from the "
            "purchase order. weight_kg and volume_cbm are").font = NOTE
    ws.cell(_ng + 5, 1,
            "per line, because two of the four usual charges are spread "
            "on them rather than on value.").font = NOTE
    ws.cell(_ng + 6, 1,
            "This adds stock. The Stock sheet is your count BEFORE these "
            "landed -- do not list the same goods twice.").font = NOTE

    # ---------------------------------------------------------- Customers
    ws = wb.create_sheet("Customers")
    head(ws, ["customer_code", "customer_name", "city", "is_public_sector",
              "is_intercompany", "payment_terms_days", "contact_name",
              "contact_email"],
         [16, 30, 16, 17, 16, 19, 22, 26],
         required=("customer_code", "customer_name"))
    for r, c in enumerate(customers, 2):
        put(ws, r, [c["code"], c["name"], c["city"],
                    "yes" if c["public"] else "",
                    "yes" if c["inter"] else "", c["terms"],
                    c["contact"], c["email"]],
            formats={6: QTY})
    ws.cell(len(customers) + 3, 1,
            "is_public_sector: government hospitals pay late as a matter "
            "of course, and mixing them into one").font = NOTE
    ws.cell(len(customers) + 4, 1,
            "figure hides whether the private book is healthy. "
            "is_intercompany: a sale to a company you own is").font = NOTE
    ws.cell(len(customers) + 5, 1,
            "not money owed -- counted in the total it flatters the "
            "figure and nobody notices.").font = NOTE

    # ----------------------------------------------------------- Invoices
    ws = wb.create_sheet("Invoices")
    head(ws, ["invoice_no", "customer_code", "branch_code", "invoice_date",
              "due_date", "amount", "amount_paid"],
         [18, 16, 13, 14, 14, 16, 16],
         required=("invoice_no", "customer_code", "invoice_date",
                   "due_date", "amount"))
    for r, i in enumerate(invoices, 2):
        put(ws, r, [i["no"], i["customer"], i["branch"], i["date"],
                    i["due"], i["amount"], i["paid"]],
            formats={6: MONEY, 7: MONEY})
    ws.cell(len(invoices) + 3, 1,
            "amount_paid, not a status: a status cannot express a part "
            "payment, and a part payment is most of this book.").font = NOTE

    # ----------------------------------------------------------- Machines
    ws = wb.create_sheet("Machines")
    head(ws, ["machine_key", "model", "serial_no", "customer_code",
              "branch_code", "placed_date", "capital_cost",
              "contract_months", "expected_monthly_tests",
              "revenue_per_test", "reagent_margin_pct"],
         [15, 22, 16, 16, 13, 14, 16, 17, 22, 17, 19],
         required=("machine_key", "model"))
    for r, m in enumerate(machines, 2):
        put(ws, r, [m["key"], m["model"], m["serial"], m["customer"],
                    m["branch"], m["placed"], m["capital"], m["months"],
                    m["tests"], m["per_test"], m["margin"]],
            formats={7: MONEY, 8: QTY, 9: QTY, 10: MONEY, 11: '0.00'})
    ws.cell(len(machines) + 3, 1,
            "capital_cost is what it cost you to put it there -- zero for "
            "a machine the customer bought, and the whole").font = NOTE
    ws.cell(len(machines) + 4, 1,
            "point of the record for one you placed free. No other system "
            "you own holds this.").font = NOTE

    # ------------------------------------------------------------ Fitment
    #
    # Which analyser consumes which reagent. One row per pairing, not a
    # column on the item, because a reagent commonly fits several
    # machines and a column would force somebody to pick one and be
    # wrong.
    ws = wb.create_sheet("Fitment")
    head(ws, ["sku", "machine_key"], [16, 15],
         required=("sku", "machine_key"))
    for r, f in enumerate(extra.get("fitment", []), 2):
        put(ws, r, [f["sku"], f["machine"]])
    _nf = len(extra.get("fitment", []))
    ws.cell(_nf + 3, 1,
            "This is what makes a slow-moving reagent readable: dead "
            "stock, or the only consumable for a machine you").font = NOTE
    ws.cell(_nf + 4, 1,
            "placed last month. List a SKU once per machine it fits. "
            "Leave a generic item out and tick is_generic").font = NOTE
    ws.cell(_nf + 5, 1,
            "on the Items sheet instead.").font = NOTE

    # ------------------------------------------------------- Service jobs
    ws = wb.create_sheet("Service jobs")
    head(ws, ["job_no", "machine_key", "branch_code", "opened_date",
              "closed_date", "labour_hours", "labour_hours_billed",
              "parts_cost", "parts_charged", "is_warranty",
              "is_repeat_visit", "downtime_days"],
         [14, 15, 13, 14, 14, 15, 20, 14, 16, 14, 17, 15],
         required=("job_no", "opened_date"))
    for r, j in enumerate(jobs, 2):
        put(ws, r, [j["no"], j["machine"], j["branch"], j["opened"],
                    j["closed"], j["hours"], j["billed"], j["parts"],
                    j["charged"], "yes" if j["warranty"] else "",
                    "yes" if j["repeat"] else "", j["down"]],
            formats={6: '0.0', 7: '0.0', 8: MONEY, 9: MONEY, 12: '0.0'})
    ws.cell(len(jobs) + 3, 1,
            "Hours worked and hours invoiced go in separate columns. Held "
            "as one number the gap between them -- the work").font = NOTE
    ws.cell(len(jobs) + 4, 1,
            "you did and did not charge for -- cannot be computed at "
            "all.").font = NOTE

    # ---------------------------------------------------------- Suppliers
    # ------------------------------------------------------ Trial balance
    #
    # One row per account, in natural positive amounts. Debits and
    # credits are the accountant's convention; somebody filling in a
    # sheet is far likelier to get "revenue 4,000,000" right than
    # "revenue credit 4,000,000", so the statement_line column tells
    # the engine the sign.
    #
    # statement_line is never guessed from the account name. Insurance
    # could be an expense or a prepayment, and a product that guesses
    # puts the difference in profit.
    ws = wb.create_sheet("Trial balance")
    head(ws, ["account_code", "account_name", "statement_line",
              "period_from", "period_to", "opening", "closing"],
         [14, 30, 26, 14, 14, 16, 16],
         required=("account_code", "account_name", "statement_line",
                   "period_to"))
    pfrom, pto = extra["period"]
    for r, (code, name, line, opening, closing) in enumerate(
            extra["accounts"], 2):
        put(ws, r, [code, name, line, pfrom, pto,
                    round(opening, 2), round(closing, 2)],
            formats={6: MONEY, 7: MONEY})
    n = len(extra["accounts"]) + 2
    for i, text in enumerate([
            "statement_line must be one of: revenue, cogs, payroll, opex, "
            "depreciation, finance_cost, other_income, tax, cash, "
            "receivables,",
            "inventory, other_current_assets, fixed_assets, "
            "accumulated_depreciation, payables, short_term_debt, "
            "long_term_debt, other_liabilities,",
            "tax_payable, equity, retained_earnings. Amounts are "
            "positive; the line decides the sign.",
            "",
            "Three checks run on these figures and all three are shown: "
            "the sheet balances at both ends, reserves move by profit "
            "less any dividend,",
            "and the cash flow ties. A statement that does not tie is "
            "not untidy -- it means a working-capital movement has been "
            "missed.",
    ]):
        ws.cell(n + i + 1, 1, text).font = NOTE
    ws.cell(n + 1, 6, f"=SUM(F2:F{n-1})").font = CALC
    ws.cell(n + 1, 7, f"=SUM(G2:G{n-1})").font = CALC

    # ---------------------------------------------------------- Dividends
    ws = wb.create_sheet("Dividends")
    head(ws, ["kind", "paid_on", "amount", "note"],
         [18, 14, 16, 36], required=("kind", "paid_on", "amount"))
    for r, d in enumerate(extra["dividends"], 2):
        put(ws, r, [d["kind"], d["on"], d["amount"], d["note"]],
            formats={3: MONEY})
    ws.cell(len(extra["dividends"]) + 3, 1,
            "Only needed if you paid one. Without it the reserves check "
            "fails for every company that did: reserves move by profit "
            "less the dividend.").font = NOTE

    # ------------------------------------------------- Committed payments
    ws = wb.create_sheet("Committed payments")
    head(ws, ["label", "kind", "due_date", "amount", "is_recurring",
              "recurs_every_days", "branch_code"],
         [30, 18, 14, 16, 14, 18, 14],
         required=("label", "kind", "due_date", "amount"))
    for r, c in enumerate(extra["commitments"], 2):
        put(ws, r, [c["label"], c["kind"], c["due"], c["amount"],
                    c["recurring"], c["every"], c["branch"]],
            formats={4: MONEY, 6: QTY})
    n = len(extra["commitments"]) + 3
    ws.cell(n, 1,
            "kind: lc_settlement, loan_repayment, payroll, rent, tax, "
            "supplier, other.").font = NOTE
    ws.cell(n + 1, 1,
            "A recurring payment is held once and spread across the "
            "weeks, so changing the rent changes every week at once. "
            "Without this sheet the").font = NOTE
    ws.cell(n + 2, 1,
            "thirteen-week view is fiction: the letter of credit settles "
            "and the salaries go out whatever the stock position "
            "says.").font = NOTE

    # ------------------------------------------------------- Bank balance
    ws = wb.create_sheet("Bank balance")
    head(ws, ["as_at", "balance", "account_label"],
         [14, 18, 24], required=("as_at", "balance"))
    for r, b in enumerate(extra["bank"], 2):
        put(ws, r, [b["as_at"], b["balance"], b["account"]],
            formats={2: MONEY})
    ws.cell(len(extra["bank"]) + 3, 1,
            "What the cash weeks open from. Leave it out and they open "
            "from nothing, which describes a business about to miss "
            "payroll.").font = NOTE

    # ---------------------------------------------------------- Employees
    ws = wb.create_sheet("Employees")
    head(ws, ["employee_no", "name", "department", "branch_code",
              "basic_monthly", "allowances_monthly", "medical_monthly",
              "employer_pf_rate", "employee_pf_rate", "joined"],
         [14, 24, 16, 14, 16, 18, 16, 16, 16, 14],
         required=("employee_no", "name", "basic_monthly"))
    for r, e in enumerate(extra["employees"], 2):
        put(ws, r, [e["no"], e["name"], e["dept"], e["branch"], e["basic"],
                    e["allow"], e["medical"], e["pf_er"], e["pf_ee"],
                    e["joined"]],
            formats={5: MONEY, 6: MONEY, 7: MONEY, 8: DEC, 9: DEC})
    n = len(extra["employees"]) + 3
    ws.cell(n, 1,
            "Pay is split because the deductions are: tax is on the "
            "taxable part, EOBI is charged on the minimum wage rather "
            "than on pay, and").font = NOTE
    ws.cell(n + 1, 1,
            "medical is exempt up to a tenth of basic. A single salary "
            "column cannot produce a correct payslip.").font = NOTE
    ws.cell(n + 2, 1,
            "Provident rates are fractions: five per cent is 0.05, not "
            "5. Typed as 5 it would be twenty times the real "
            "figure.").font = NOTE

    # ----------------------------------------------------------- Pipeline
    ws = wb.create_sheet("Pipeline")
    head(ws, ["reference", "customer_code", "stage", "value",
              "expected_close", "sku", "quantity", "source", "owner",
              "opened", "closed"],
         [18, 16, 20, 16, 16, 16, 12, 14, 20, 14, 14],
         required=("reference", "stage", "value"))
    for r, o in enumerate(extra["pipeline"], 2):
        put(ws, r, [o["ref"], o["customer"], o["stage"], o["value"],
                    o["close"], o["sku"], o["qty"], o["source"],
                    o["owner"], o["opened"], o["closed"]],
            formats={4: MONEY, 7: QTY})
    n = len(extra["pipeline"]) + 3
    ws.cell(n, 1,
            "stage: lead, qualified, quoted, tender_submitted, won, "
            "lost. Won and lost rows are what the win rate is measured "
            "from, so keep them.").font = NOTE
    ws.cell(n + 1, 1,
            "The sku is what lets a tender be checked against the "
            "shelf. One closing in six weeks for a line with eleven "
            "weeks of lead time and").font = NOTE
    ws.cell(n + 2, 1,
            "nothing on hand is not a forecast, it is a disappointment "
            "with a date on it.").font = NOTE

    # --------------------------------------------------- Marketing spend
    ws = wb.create_sheet("Marketing spend")
    head(ws, ["label", "category", "spent_on", "amount"],
         [34, 18, 14, 16], required=("label", "spent_on", "amount"))
    for r, x in enumerate(extra["spend"], 2):
        put(ws, r, [x["label"], x["category"], x["on"], x["amount"]],
            formats={4: MONEY})
    ws.cell(len(extra["spend"]) + 3, 1,
            "Spend against wins gives a cost per win, which is the only "
            "marketing figure a small business owner can act on.").font = NOTE

    # ----------------------------------------------------------- Contacts
    ws = wb.create_sheet("Contacts")
    head(ws, ["customer_code", "supplier_name", "name", "role",
              "email", "phone", "whatsapp", "is_primary"],
         [16, 24, 24, 24, 26, 18, 18, 12], required=("name",))
    for r, c in enumerate(extra["contacts"], 2):
        put(ws, r, [c["customer"], c["supplier"], c["name"], c["role"],
                    c["email"], c["phone"], c["whatsapp"], c["primary"]])
    ws.cell(len(extra["contacts"]) + 3, 1,
            "One of customer_code or supplier_name, never both and "
            "never neither -- otherwise there is no answer to whose "
            "contact this is.").font = NOTE

    # ----------------------------------------------------------- Dealings
    ws = wb.create_sheet("Dealings")
    head(ws, ["customer_code", "happened_on", "channel", "summary",
              "next_step", "next_step_on"],
         [16, 14, 14, 56, 30, 16],
         required=("customer_code", "happened_on", "summary"))
    for r, d in enumerate(extra["dealings"], 2):
        put(ws, r, [d["customer"], d["on"], d["channel"], d["summary"],
                    d["next"], d["next_on"]])
    n = len(extra["dealings"]) + 3
    ws.cell(n, 1,
            "channel: note, call, visit, email, whatsapp, "
            "meeting.").font = NOTE
    ws.cell(n + 1, 1,
            "The last thing anybody said to a customer, attached to the "
            "customer it was about. This is the half of a CRM people "
            "actually use.").font = NOTE

    ws = wb.create_sheet("Suppliers")
    head(ws, ["supplier_name", "country", "currency", "contact_email"],
         [28, 18, 12, 28], required=("supplier_name",))
    for r, (name, country, ccy) in enumerate(suppliers, 2):
        put(ws, r, [name, country, ccy, ""])
    ws.cell(len(suppliers) + 3, 1,
            "Optional. Leave this sheet empty and suppliers are created "
            "from the names on Items.").font = NOTE

    # -------------------------------------------------------------- Check
    ws = wb.create_sheet("Check")
    ws.column_dimensions["A"].width = 54
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 54
    for i, h in enumerate(["What is checked", "Count", "Verdict",
                           "What to do"], 1):
        c = ws.cell(1, i, h)
        c.font = HEAD
        c.fill = NAVY

    ni, ns, nh, nb = (len(items) + 1, len(stock) + 1, len(history) + 1,
                      len(branches) + 1)
    checks = [
        ("Branches", f"=COUNTA(Branches!A2:A{nb})", "count",
         "Every branch you hold stock at."),
        ("Items", f"=COUNTA(Items!A2:A{ni})", "count",
         "One row per line you carry."),
        ("Stock rows", f"=COUNTA(Stock!A2:A{ns})", "count",
         "One row per item per branch."),
        ("Duplicate SKUs on Items",
         f"=SUMPRODUCT((COUNTIF(Items!A2:A{ni},Items!A2:A{ni})>1)"
         f"*(Items!A2:A{ni}<>\"\"))", "zero",
         "Each SKU once. Two rows for one SKU means the importer has to "
         "guess which is true."),
        ("Stock rows whose SKU is not on Items",
         f"=SUMPRODUCT((Stock!A2:A{ns}<>\"\")"
         f"*(COUNTIF(Items!$A$2:$A${ni},Stock!A2:A{ns})=0))", "zero",
         "Add the item to Items, or fix the spelling here."),
        ("Stock rows whose branch is not on Branches",
         f"=SUMPRODUCT((Stock!B2:B{ns}<>\"\")"
         f"*(COUNTIF(Branches!$A$2:$A${nb},Stock!B2:B{ns})=0))", "zero",
         "Add the branch to Branches, or fix the code here."),
        ("Negative stock", f"=COUNTIF(Stock!C2:C{ns},\"<0\")", "zero",
         "A count cannot be below zero. Check for a typed minus sign."),
        ("Items with no sales history",
         f"=SUMPRODUCT((Items!A2:A{ni}<>\"\")"
         f"*(COUNTIF('Sales history'!$A$2:$A${nh},Items!A2:A{ni})=0))",
         "warn",
         "They will import, but with no demand there is no cover, no "
         "reorder point and nothing to decide."),
        ("Total stock at cost", f"=SUM(Stock!E2:E{ns})", "money",
         "Sense-check this against your own figure before importing."),
    ]

    # The money sheets, checked here rather than only after upload. The
    # balance-sheet test is the one worth having in Excel: it is the
    # single most common thing wrong with a hand-built trial balance,
    # and finding it here costs a minute rather than an import.
    na = len(extra["accounts"]) + 1
    npl = len(extra["pipeline"]) + 1
    nd = len(extra["dealings"]) + 1
    nc = len(customers) + 1
    if extra["accounts"]:
        checks += [
            ("Trial balance: assets less liabilities and equity, closing",
             "=ROUND(SUMIF('Trial balance'!$C$2:$C$" + str(na) +
             ',"cash",\'Trial balance\'!$G$2:$G$' + str(na) + ")"
             "+SUMIF('Trial balance'!$C$2:$C$" + str(na) +
             ',"receivables",\'Trial balance\'!$G$2:$G$' + str(na) + ")"
             "+SUMIF('Trial balance'!$C$2:$C$" + str(na) +
             ',"inventory",\'Trial balance\'!$G$2:$G$' + str(na) + ")"
             "+SUMIF('Trial balance'!$C$2:$C$" + str(na) +
             ',"other_current_assets",\'Trial balance\'!$G$2:$G$'
             + str(na) + ")"
             "+SUMIF('Trial balance'!$C$2:$C$" + str(na) +
             ',"fixed_assets",\'Trial balance\'!$G$2:$G$' + str(na) + ")"
             "-SUMIF('Trial balance'!$C$2:$C$" + str(na) +
             ',"accumulated_depreciation",\'Trial balance\'!$G$2:$G$'
             + str(na) + ")"
             "-SUMIF('Trial balance'!$C$2:$C$" + str(na) +
             ',"payables",\'Trial balance\'!$G$2:$G$' + str(na) + ")"
             "-SUMIF('Trial balance'!$C$2:$C$" + str(na) +
             ',"short_term_debt",\'Trial balance\'!$G$2:$G$' + str(na) + ")"
             "-SUMIF('Trial balance'!$C$2:$C$" + str(na) +
             ',"long_term_debt",\'Trial balance\'!$G$2:$G$' + str(na) + ")"
             "-SUMIF('Trial balance'!$C$2:$C$" + str(na) +
             ',"other_liabilities",\'Trial balance\'!$G$2:$G$' + str(na) + ")"
             "-SUMIF('Trial balance'!$C$2:$C$" + str(na) +
             ',"tax_payable",\'Trial balance\'!$G$2:$G$' + str(na) + ")"
             "-SUMIF('Trial balance'!$C$2:$C$" + str(na) +
             ',"equity",\'Trial balance\'!$G$2:$G$' + str(na) + ")"
             "-SUMIF('Trial balance'!$C$2:$C$" + str(na) +
             ',"retained_earnings",\'Trial balance\'!$G$2:$G$'
             + str(na) + "),0)",
             "zero",
             "Must be nil. The commonest thing wrong with a hand-built "
             "trial balance, and the app will say so too -- but here it "
             "costs a minute instead of an import."),
            ("Pipeline rows whose sku is not on Items",
             f"=SUMPRODUCT((Pipeline!F2:F{npl}<>\"\")"
             f"*(COUNTIF(Items!$A$2:$A${ni},Pipeline!F2:F{npl})=0))",
             "warn",
             "They import, but a tender with no item cannot be checked "
             "against the shelf, which is the point of recording it "
             "here."),
            ("Dealings against a customer not on Customers",
             f"=SUMPRODUCT((Dealings!A2:A{nd}<>\"\")"
             f"*(COUNTIF(Customers!$A$2:$A${nc},Dealings!A2:A{nd})=0))",
             "zero",
             "These are skipped on import: there is nobody to attach "
             "them to."),
        ]
    for r, (label, formula, kind, advice) in enumerate(checks, 2):
        ws.cell(r, 1, label).font = BODY
        c = ws.cell(r, 2, formula)
        c.font = CALC
        c.number_format = MONEY if kind == "money" else QTY
        if kind == "zero":
            v = ws.cell(r, 3, f'=IF(B{r}=0,"ok","fix this")')
        elif kind == "warn":
            v = ws.cell(r, 3, f'=IF(B{r}=0,"ok","worth a look")')
        else:
            v = ws.cell(r, 3, "—")
        v.font = CALC
        ws.cell(r, 4, advice).font = BODY
        ws.cell(r, 4).alignment = Alignment(wrap_text=True, vertical="top")
        for col in range(1, 5):
            ws.cell(r, col).border = RULE
    ws.cell(len(checks) + 3, 1,
            "These are counts, not permission. They catch the problems "
            "that stop an import, not a wrong number.").font = NOTE

    return wb


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "."
    for i, key in enumerate(("meridian", "indus")):
        wb = build(key, seed=11 + i)
        name = ORGS[key]["name"].lower().replace(" ", "-")
        path = f"{out}/landed-import-{name}.xlsx"
        wb.save(path)
        print("wrote", path)
