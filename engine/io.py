"""Minimal CSV loading for phase 1.

This is deliberately dumb. Real column mapping, validation and the data
quality report belong in the ingestion layer, which is a later step. For
now the engine reads already-canonical CSV so the calculation modules can
be proven first.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import List

from engine.schema import DemandPeriod, ItemSnapshot


def _f(row: dict, key: str, default: float = 0.0) -> float:
    raw = (row.get(key) or "").strip()
    return float(raw) if raw else default


def _d(row: dict, key: str) -> date | None:
    raw = (row.get(key) or "").strip()
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d").date()


def load_items(path: str | Path) -> List[ItemSnapshot]:
    with open(path, newline="", encoding="utf-8") as fh:
        return [
            ItemSnapshot(
                item_key=row["item_key"].strip(),
                description=row.get("description", "").strip(),
                category=row.get("category", "").strip(),
                on_hand=_f(row, "on_hand"),
                open_po=_f(row, "open_po"),
                unit_cost=_f(row, "unit_cost"),
                lead_time_days=_f(row, "lead_time_days"),
                lead_time_days_std=_f(row, "lead_time_days_std"),
                is_clinical_critical=(
                    row.get("is_clinical_critical", "").strip().lower()
                    in ("1", "true", "yes", "y")
                ),
                expiry_date=_d(row, "expiry_date"),
                entity_id=row.get("entity_id", "default").strip() or "default",
                location_id=row.get("location_id", "default").strip() or "default",
            )
            for row in csv.DictReader(fh)
        ]


def load_history(path: str | Path) -> List[DemandPeriod]:
    with open(path, newline="", encoding="utf-8") as fh:
        return [
            DemandPeriod(
                item_key=row["item_key"].strip(),
                period=row["period"].strip(),
                quantity=_f(row, "quantity"),
                entity_id=row.get("entity_id", "default").strip() or "default",
                location_id=row.get("location_id", "default").strip() or "default",
            )
            for row in csv.DictReader(fh)
        ]
