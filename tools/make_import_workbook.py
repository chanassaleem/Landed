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
        orders.append({
            "po": f"PO-{today.year}-{1000+n}",
            "supplier": sup[0], "branch": code, "sku": it["sku"],
            "qty": max(1, int(it["base"] * rng.uniform(3, 10))),
            "price": round(it["cost"] * rng.uniform(0.8, 0.95), -1),
            "currency": sup[2],
            "fx": 282.0 if sup[2] == "USD" else 305.0,
            "ordered": ordered.isoformat(),
            "expected": (ordered + timedelta(days=it["lead"])).isoformat(),
            "status": rng.choice(["placed", "placed", "part_received"]),
        })

    # ---- shipments and their costs
    shipments = []
    for n in range(4):
        po = orders[n]["po"]
        ref = f"SHP-{today.year}-{200+n}"
        etd = today - timedelta(days=rng.randint(2, 30))
        eta = etd + timedelta(days=rng.randint(18, 45))
        for cost_type, basis, amount in (
            ("Ocean freight", "value", rng.randint(80_000, 260_000)),
            ("Customs duty", "value", rng.randint(120_000, 420_000)),
            ("Clearing agent", "qty", rng.randint(18_000, 45_000)),
            ("Inland transport", "weight", rng.randint(12_000, 38_000)),
        ):
            shipments.append({
                "ref": ref, "po": po, "mode": rng.choice(["sea", "air"]),
                "etd": etd.isoformat(), "eta": eta.isoformat(),
                "cost_type": cost_type, "amount": amount,
                "allocate_by": basis,
            })

    return render(spec, branches, suppliers, items, stock, history,
                  orders, shipments, months)


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
           shipments, months):
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
        ("The sheets Landed reads when you import. Fill them in, upload the "
         "file, and every figure", BODY),
        ("in the app is yours. The example rows are invented -- replace "
         "them with your own.", BODY),
        ("", BODY),
        ("Fill these in", H2),
        ("Branches      every branch and warehouse you hold stock at.", BODY),
        ("Items         every line you carry. Once each, not once per "
         "branch.", BODY),
        ("Stock         what is on the shelf today, per item per branch.", BODY),
        ("Sales history what you sold each month. Months run across the "
         "top.", BODY),
        ("", BODY),
        ("Fill these in if you have them", H2),
        ("On order      purchase orders placed and not yet arrived.", BODY),
        ("Shipments     what is in transit, and the freight and duty on "
         "it.", BODY),
        ("Suppliers     who you buy from. Skipped, they are created from "
         "the names on Items.", BODY),
        ("", BODY),
        ("Two rules worth knowing", H2),
        ("A blank month on Sales history means you sold none that month. "
         "That is a fact and the", BODY),
        ("forecast uses it. If you genuinely have no record for a month, "
         "delete the whole column", BODY),
        ("instead -- a zero and a gap mean different things, and only one "
         "of them is true.", BODY),
        ("", BODY),
        ("An item's lead time, supplier and description live on Items and "
         "nowhere else. That is why", BODY),
        ("Items and Stock are separate sheets: stated once, they cannot "
         "disagree with themselves.", BODY),
        ("", BODY),
        ("A column marked * is required. Blue text is something you type.", NOTE),
        ("Check the Check sheet before you upload. It counts the problems "
         "that stop an import.", NOTE),
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
              "is_clinical_critical"],
         [16, 34, 16, 24, 10, 15, 18, 18],
         required=("sku", "description", "lead_time_days"))
    for r, it in enumerate(items, 2):
        put(ws, r, [it["sku"], it["description"], it["category"],
                    it["supplier"], it["uom"], it["lead"], it["lead_std"],
                    it["critical"]],
            formats={6: QTY, 7: QTY})
    ws.cell(len(items) + 3, 1,
            "lead_time_days is order to shelf. lead_time_days_std is how "
            "much that varies -- blank means steady.").font = NOTE

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
              "cost_type", "amount_pkr", "allocate_by"],
         [16, 16, 10, 13, 13, 20, 14, 14],
         required=("shipment_ref", "po_number", "cost_type", "amount_pkr",
                   "allocate_by"))
    for r, s in enumerate(shipments, 2):
        put(ws, r, [s["ref"], s["po"], s["mode"], s["etd"], s["eta"],
                    s["cost_type"], s["amount"], s["allocate_by"]],
            formats={7: MONEY})
    ws.cell(len(shipments) + 3, 1,
            "One row per cost, not per shipment: a shipping line bills by "
            "volume and customs by value, so each").font = NOTE
    ws.cell(len(shipments) + 4, 1,
            "cost says how it is spread. allocate_by: value, weight, "
            "volume or qty.").font = NOTE

    # ---------------------------------------------------------- Suppliers
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
