"""Run the engine from a terminal. No UI, no database, no auth.

    python -m engine.cli tests/data/inventory.csv tests/data/demand_history.csv
"""

from __future__ import annotations

import argparse
from datetime import date, datetime

from engine.io import load_history, load_items
from engine.modules.classify import classify
from engine.modules.inventory import evaluate
from engine.schema import StockStatus, Thresholds


def main() -> None:
    ap = argparse.ArgumentParser(description="S&OP engine, phase 1")
    ap.add_argument("inventory_csv")
    ap.add_argument("history_csv")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD")
    ap.add_argument("--excess-months", type=float, default=6.0)
    ap.add_argument("--target-cover", type=float, default=3.0)
    args = ap.parse_args()

    as_of = (
        datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else date.today()
    )
    thresholds = Thresholds(
        target_cover_months=args.target_cover,
        excess_cover_months=args.excess_months,
    )

    items = load_items(args.inventory_csv)
    history = load_history(args.history_csv)
    classes = classify(items, history, thresholds)
    results = evaluate(items, classes, thresholds, as_of=as_of)

    print(f"\nS&OP engine · {len(items)} items · as of {as_of}\n")
    head = (
        f"{'ITEM':<12}{'CLASS':<14}{'ABC':<5}{'TGT':>6}{'ONHAND':>9}"
        f"{'ROP':>9}{'SS':>8}{'ORDER':>9}  STATUS"
    )
    print(head)
    print("-" * len(head))
    for r in results:
        print(
            f"{r.item_key:<12}{r.demand_class.value:<14}{r.abc_class.value:<5}"
            f"{r.service_target:>6.0%}{r.on_hand:>9,.0f}{r.reorder_point:>9,.0f}"
            f"{r.safety_stock:>8,.0f}{r.suggested_order_qty:>9,.0f}  {r.status.value}"
        )

    order_value = sum(
        r.suggested_order_qty * next(i.unit_cost for i in items if i.item_key == r.item_key)
        for r in results
    )
    stock_value = sum(r.stock_value for r in results)
    trapped = sum(r.excess_value for r in results)
    critical = [r for r in results if r.status == StockStatus.CRITICAL]

    print(f"\nStock value          PKR {stock_value:,.0f}")
    print(f"Cash trapped         PKR {trapped:,.0f}")
    print(f"Suggested PO value   PKR {order_value:,.0f}")
    if order_value > stock_value:
        print(
            "  ! Suggested orders exceed the value of stock on hand. "
            "The cash module will need to sequence these."
        )
    print(f"\nNeeds action now ({len(critical)}):")
    for r in critical:
        print(f"  {r.item_key}  {r.description}")
        print(f"      {r.reason}")


if __name__ == "__main__":
    main()
