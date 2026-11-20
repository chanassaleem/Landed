"""DuckDB store for the S&OP engine.

The store owns persistence and nothing else. It never calculates. The
calculation modules stay pure functions over in-memory records, which is
what keeps them testable without a database.

Design rules enforced here:
  - Facts are append-only. There is no update path for a fact row.
  - A re-upload of the same (entity, location, report_type, as_of_date)
    supersedes the previous snapshot rather than overwriting it.
  - The same file uploaded twice is detected by SHA-256 and refused as a
    duplicate rather than silently double-counted.
  - Every metric run records its snapshot lineage and threshold set.
"""

from __future__ import annotations

import hashlib
import json
import uuid
import functools
import inspect
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import duckdb

from engine import __version__
from engine.schema import (
    DemandPeriod,
    InventoryResult,
    ItemSnapshot,
    Thresholds,
)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

REPORT_INVENTORY = "inventory"
REPORT_DEMAND = "demand"


def _iso(row):
    """Datetimes and dates as ISO strings, so a row is JSON-safe."""
    return [
        v.isoformat() if hasattr(v, "isoformat") else v
        for v in row
    ]


def _uid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DuplicateUpload(Exception):
    """Raised when the identical file has already been ingested."""

    def __init__(self, snapshot_id: str, uploaded_at: datetime):
        self.snapshot_id = snapshot_id
        self.uploaded_at = uploaded_at
        super().__init__(
            f"This exact file was already uploaded at {uploaded_at:%Y-%m-%d %H:%M} UTC"
        )


class Store:
    """Thin persistence layer over DuckDB.

    Pass path=':memory:' for tests, or a file path for the desktop product.
    """

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.con = duckdb.connect(self.path)
        # One connection is shared by every request, and the web server runs
        # synchronous endpoints in a threadpool, so two requests can reach it
        # at once. DuckDB refuses a second call on a busy connection rather
        # than waiting for it, which surfaced as an intermittent 500 on
        # /api/meta while the page was booting: four requests go out together
        # and whichever lost the race emptied the navigation rail.
        #
        # Reentrant because methods call one another (save_entity logs, and
        # the logging writes), and held across the fetch as well as the
        # execute: a DuckDB result is read from the connection lazily, so
        # releasing between the two would reopen the same race.
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        self.con.execute(SCHEMA_PATH.read_text())
        self._migrate()

    def _migrate(self) -> None:
        """CREATE TABLE IF NOT EXISTS leaves an older database on its old
        columns, so each added column is applied here too. Adding one twice
        is an error rather than a no-op in DuckDB, hence the catch."""
        for table, column, ddl in (
            ("entity", "configured", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("location", "configured", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ):
            have = {r[0] for r in self.con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = ?", [table]).fetchall()}
            if column in have:
                continue
            self.con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            # A row already there was created before the distinction existed.
            # Treating it as configured is the safe reading: it may well have
            # been set up by hand, and nothing is lost by leaving it listed.
            self.con.execute(f"UPDATE {table} SET {column} = TRUE")
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    @contextmanager
    def tx(self):
        self.con.execute("BEGIN TRANSACTION")
        try:
            yield
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------ master

    def ensure_entity(
        self,
        entity_id: str,
        legal_name: str | None = None,
        is_separate_company: bool = True,
    ) -> str:
        found = self.con.execute(
            "SELECT entity_id FROM entity WHERE entity_id = ?", [entity_id]
        ).fetchone()
        if not found:
            self.con.execute(
                """INSERT INTO entity (entity_id, legal_name, ntn,
                       is_separate_company, base_currency, created_at, configured)
                   VALUES (?, ?, NULL, ?, 'PKR', ?, FALSE)""",
                [entity_id, legal_name or entity_id, is_separate_company, _now()],
            )
        return entity_id

    def ensure_location(
        self, location_id: str, entity_id: str, name: str | None = None
    ) -> str:
        found = self.con.execute(
            "SELECT location_id FROM location WHERE location_id = ?", [location_id]
        ).fetchone()
        if not found:
            self.ensure_entity(entity_id)
            self.con.execute(
                """INSERT INTO location (location_id, entity_id, name, city,
                       is_warehouse, configured)
                   VALUES (?, ?, ?, NULL, TRUE, FALSE)""",
                [location_id, entity_id, name or location_id],
            )
        return location_id

    def upsert_items(self, items: Iterable[ItemSnapshot]) -> int:
        n = 0
        for it in items:
            self.con.execute(
                """INSERT INTO item (item_key, canonical_code, description, category,
                                     is_clinical_critical)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (item_key) DO UPDATE SET
                     description = excluded.description,
                     category = excluded.category,
                     is_clinical_critical = excluded.is_clinical_critical""",
                [
                    it.item_key,
                    it.item_key,
                    it.description,
                    it.category,
                    it.is_clinical_critical,
                ],
            )
            n += 1
        return n

    # --------------------------------------------------------- ingestion

    def add_snapshot(
        self,
        *,
        report_type: str,
        as_of: date,
        entity_id: str = "default",
        location_id: str = "default",
        source_filename: str = "",
        source_bytes: bytes = b"",
        uploaded_by: str = "system",
        row_count: int = 0,
        rows_rejected: int = 0,
        status: str = "valid",
    ) -> str:
        """Create a snapshot header and supersede any prior one for the key."""
        digest = sha256_of(source_bytes) if source_bytes else None

        if digest:
            dup = self.con.execute(
                """SELECT snapshot_id, uploaded_at FROM snapshot
                   WHERE source_sha256 = ? AND superseded_by IS NULL""",
                [digest],
            ).fetchone()
            if dup:
                raise DuplicateUpload(dup[0], dup[1])

        self.ensure_location(location_id, entity_id)
        snapshot_id = _uid()

        # Supersede, never overwrite. The old rows stay for audit.
        self.con.execute(
            """UPDATE snapshot SET superseded_by = ?
               WHERE entity_id = ? AND location_id = ? AND report_type = ?
                 AND as_of_date = ? AND superseded_by IS NULL""",
            [snapshot_id, entity_id, location_id, report_type, as_of],
        )
        self.con.execute(
            """INSERT INTO snapshot VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL)""",
            [
                snapshot_id, entity_id, location_id, report_type, as_of,
                _now(), uploaded_by, source_filename, digest,
                len(source_bytes) if source_bytes else 0,
                row_count, rows_rejected, status,
            ],
        )
        self.log(
            "upload", "snapshot", snapshot_id, uploaded_by,
            {"report_type": report_type, "as_of": as_of.isoformat(),
             "rows": row_count, "rejected": rows_rejected},
        )
        return snapshot_id

    def add_inventory_facts(
        self, snapshot_id: str, items: Sequence[ItemSnapshot]
    ) -> int:
        self.con.executemany(
            "INSERT INTO fact_inventory VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [snapshot_id, i.item_key, i.on_hand, i.open_po, i.unit_cost,
                 i.lead_time_days, i.lead_time_days_std, i.expiry_date]
                for i in items
            ],
        )
        return len(items)

    def add_demand_facts(
        self, snapshot_id: str, periods: Sequence[DemandPeriod]
    ) -> int:
        self.con.executemany(
            "INSERT INTO fact_demand VALUES (?, ?, ?, ?)",
            [[snapshot_id, p.item_key, p.period, p.quantity] for p in periods],
        )
        return len(periods)

    def reject_row(
        self, snapshot_id: str, row_number: int, reason: str, raw: dict
    ) -> None:
        self.con.execute(
            "INSERT INTO rejected_row VALUES (?, ?, ?, ?)",
            [snapshot_id, row_number, reason, json.dumps(raw)[:4000]],
        )

    # ------------------------------------------------------------ reads

    def latest_snapshot_id(
        self,
        report_type: str,
        entity_id: str = "default",
        location_id: str = "default",
        on_or_before: Optional[date] = None,
    ) -> Optional[str]:
        sql = """SELECT snapshot_id FROM current_snapshot
                 WHERE report_type = ? AND entity_id = ? AND location_id = ?"""
        args: list = [report_type, entity_id, location_id]
        if on_or_before:
            sql += " AND as_of_date <= ?"
            args.append(on_or_before)
        sql += " ORDER BY as_of_date DESC, uploaded_at DESC LIMIT 1"
        row = self.con.execute(sql, args).fetchone()
        return row[0] if row else None

    def read_items(self, snapshot_id: str) -> List[ItemSnapshot]:
        rows = self.con.execute(
            """SELECT f.item_key, COALESCE(i.description, ''), COALESCE(i.category, ''),
                      f.on_hand, f.open_po, f.unit_cost, f.lead_time_days,
                      f.lead_time_days_std, COALESCE(i.is_clinical_critical, FALSE),
                      f.expiry_date, s.entity_id, s.location_id
               FROM fact_inventory f
               JOIN snapshot s ON s.snapshot_id = f.snapshot_id
               LEFT JOIN item i ON i.item_key = f.item_key
               WHERE f.snapshot_id = ?
               ORDER BY f.item_key""",
            [snapshot_id],
        ).fetchall()
        return [
            ItemSnapshot(
                item_key=r[0], description=r[1], category=r[2], on_hand=r[3],
                open_po=r[4], unit_cost=r[5], lead_time_days=r[6],
                lead_time_days_std=r[7], is_clinical_critical=bool(r[8]),
                expiry_date=r[9], entity_id=r[10], location_id=r[11],
            )
            for r in rows
        ]

    def read_demand(self, snapshot_id: str) -> List[DemandPeriod]:
        rows = self.con.execute(
            """SELECT item_key, period, quantity FROM fact_demand
               WHERE snapshot_id = ? ORDER BY item_key, period""",
            [snapshot_id],
        ).fetchall()
        return [DemandPeriod(item_key=r[0], period=r[1], quantity=r[2]) for r in rows]

    def snapshots(self, limit: int = 50) -> List[dict]:
        rows = self.con.execute(
            """SELECT snapshot_id, entity_id, location_id, report_type, as_of_date,
                      uploaded_at, source_filename, row_count, rows_rejected,
                      status, superseded_by
               FROM snapshot ORDER BY uploaded_at DESC LIMIT ?""",
            [limit],
        ).fetchall()
        keys = ["snapshot_id", "entity_id", "location_id", "report_type", "as_of_date",
                "uploaded_at", "source_filename", "row_count", "rows_rejected",
                "status", "superseded_by"]
        return [dict(zip(keys, r)) for r in rows]

    # ------------------------------------------------------- metric runs

    def save_thresholds(
        self, thresholds: Thresholds, entity_id: str = "default", by: str = "system"
    ) -> str:
        payload = asdict(thresholds)
        payload["service_targets"] = {
            f"{k[0]}|{k[1]}": v for k, v in thresholds.service_targets.items()
        }
        blob = json.dumps(payload, sort_keys=True)

        found = self.con.execute(
            """SELECT threshold_set_id FROM threshold_set
               WHERE entity_id = ? AND payload_json = ?""",
            [entity_id, blob],
        ).fetchone()
        if found:
            return found[0]

        tsid = _uid()
        self.con.execute(
            "INSERT INTO threshold_set VALUES (?, ?, ?, ?, ?)",
            [tsid, entity_id, _now(), by, blob],
        )
        self.log("threshold_change", "threshold_set", tsid, by, payload)
        return tsid

    def save_metric_run(
        self,
        results: Sequence[InventoryResult],
        *,
        as_of: date,
        threshold_set_id: str,
        snapshot_ids: Sequence[str],
        order_values: dict,
        entity_id: str = "default",
        location_id: str = "default",
    ) -> str:
        run_id = _uid()
        self.con.execute(
            "INSERT INTO metric_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, entity_id, location_id, as_of, _now(), threshold_set_id,
             json.dumps(list(snapshot_ids)), __version__, len(results)],
        )
        self.con.executemany(
            "INSERT INTO metric_result VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                [run_id, r.item_key, r.demand_class.value, r.abc_class.value,
                 r.service_target, r.service_metric.value, r.safety_stock,
                 r.reorder_point, r.order_up_to, r.suggested_order_qty,
                 order_values.get(r.item_key, 0.0), r.months_cover,
                 r.status.value, r.stock_value, r.excess_value,
                 r.days_to_expiry, r.reason]
                for r in results
            ],
        )
        return run_id

    def runs(self, limit: int = 30) -> List[dict]:
        rows = self.con.execute(
            """SELECT r.run_id, r.as_of_date, r.created_at, r.item_count,
                      r.engine_version,
                      SUM(m.stock_value), SUM(m.order_value), SUM(m.excess_value),
                      SUM(CASE WHEN m.status = 'CRITICAL' THEN 1 ELSE 0 END)
               FROM metric_run r JOIN metric_result m ON m.run_id = r.run_id
               GROUP BY ALL ORDER BY r.created_at DESC LIMIT ?""",
            [limit],
        ).fetchall()
        keys = ["run_id", "as_of_date", "created_at", "item_count", "engine_version",
                "stock_value", "order_value", "excess_value", "critical"]
        return [dict(zip(keys, r)) for r in rows]

    def run_results(self, run_id: str) -> List[dict]:
        rows = self.con.execute(
            "SELECT * FROM metric_result WHERE run_id = ?", [run_id]
        ).fetchall()
        cols = [d[0] for d in self.con.description]
        return [dict(zip(cols, r)) for r in rows]

    # ------------------------------------------------------------ audit


    # ------------------------------------------------- master data (config)
    #
    # The schema has carried entity, location, customer, item and alias from
    # the start, and nothing has ever written to them except the stubs that
    # ensure_entity and ensure_location create during an upload. That is why
    # a company and a branch have been strings in a CSV row rather than
    # objects anybody could name or configure. These are the methods that
    # make them real.

    def group(self) -> dict:
        """The owning group. One row, because an install belongs to one owner."""
        row = self.con.execute(
            "SELECT payload_json FROM threshold_set WHERE threshold_set_id = 'group'"
        ).fetchone()
        if not row:
            return {"name": "", "country": "PK", "currency": "PKR"}
        return json.loads(row[0])

    def save_group(self, name: str, country: str = "PK",
                   currency: str = "PKR", actor: str = "system") -> dict:
        payload = {"name": name.strip(), "country": country, "currency": currency}
        self.con.execute(
            "DELETE FROM threshold_set WHERE threshold_set_id = 'group'"
        )
        self.con.execute(
            "INSERT INTO threshold_set VALUES ('group', NULL, ?, ?, ?)",
            [_now(), actor, json.dumps(payload)],
        )
        self.log("configure", "group", "group", actor, payload)
        self.con.commit()
        return payload

    def entities(self) -> List[dict]:
        rows = self.con.execute(
            """SELECT e.entity_id, e.legal_name, e.ntn, e.is_separate_company,
                      e.base_currency, e.created_at, e.configured,
                      (SELECT COUNT(*) FROM location l WHERE l.entity_id = e.entity_id)
               FROM entity e ORDER BY e.configured DESC, e.entity_id"""
        ).fetchall()
        keys = ["entity_id", "legal_name", "ntn", "is_separate_company",
                "base_currency", "created_at", "configured", "branches"]
        return [dict(zip(keys, _iso(r))) for r in rows]

    def save_entity(
        self, entity_id: str, legal_name: str, ntn: str = "",
        is_separate_company: bool = True, base_currency: str = "PKR",
        actor: str = "system",
    ) -> dict:
        entity_id = entity_id.strip()
        if not entity_id:
            raise ValueError("A company needs a code")
        if not legal_name.strip():
            raise ValueError("A company needs a legal name")
        exists = self.con.execute(
            "SELECT 1 FROM entity WHERE entity_id = ?", [entity_id]
        ).fetchone()
        if exists:
            self.con.execute(
                """UPDATE entity SET legal_name = ?, ntn = ?,
                   is_separate_company = ?, base_currency = ?, configured = TRUE
                   WHERE entity_id = ?""",
                [legal_name.strip(), ntn.strip() or None,
                 is_separate_company, base_currency, entity_id],
            )
            action = "update"
        else:
            self.con.execute(
                """INSERT INTO entity (entity_id, legal_name, ntn,
                       is_separate_company, base_currency, created_at, configured)
                   VALUES (?, ?, ?, ?, ?, ?, TRUE)""",
                [entity_id, legal_name.strip(), ntn.strip() or None,
                 is_separate_company, base_currency, _now()],
            )
            action = "create"
        self.log(f"{action}_entity", "entity", entity_id, actor,
                 {"legal_name": legal_name, "separate_company": is_separate_company})
        self.con.commit()
        return {"entity_id": entity_id, "legal_name": legal_name.strip()}

    def locations(self) -> List[dict]:
        rows = self.con.execute(
            """SELECT l.location_id, l.entity_id, l.name, l.city, l.is_warehouse,
                      l.configured, e.legal_name
               FROM location l LEFT JOIN entity e ON e.entity_id = l.entity_id
               ORDER BY l.configured DESC, l.entity_id, l.location_id"""
        ).fetchall()
        keys = ["location_id", "entity_id", "name", "city", "is_warehouse",
                "configured", "entity_name"]
        return [dict(zip(keys, _iso(r))) for r in rows]

    def save_location(
        self, location_id: str, entity_id: str, name: str,
        city: str = "", is_warehouse: bool = True, actor: str = "system",
    ) -> dict:
        location_id, entity_id = location_id.strip(), entity_id.strip()
        if not location_id:
            raise ValueError("A branch needs a code")
        if not name.strip():
            raise ValueError("A branch needs a name")
        if not self.con.execute(
            "SELECT 1 FROM entity WHERE entity_id = ?", [entity_id]
        ).fetchone():
            raise ValueError(f"There is no company with the code {entity_id!r}")

        exists = self.con.execute(
            "SELECT 1 FROM location WHERE location_id = ?", [location_id]
        ).fetchone()
        if exists:
            self.con.execute(
                """UPDATE location SET entity_id = ?, name = ?, city = ?,
                   is_warehouse = ?, configured = TRUE WHERE location_id = ?""",
                [entity_id, name.strip(), city.strip() or None,
                 is_warehouse, location_id],
            )
            action = "update"
        else:
            self.con.execute(
                """INSERT INTO location (location_id, entity_id, name, city,
                       is_warehouse, configured)
                   VALUES (?, ?, ?, ?, ?, TRUE)""",
                [location_id, entity_id, name.strip(),
                 city.strip() or None, is_warehouse],
            )
            action = "create"
        self.log(f"{action}_location", "location", location_id, actor,
                 {"entity_id": entity_id, "name": name})
        self.con.commit()
        return {"location_id": location_id, "entity_id": entity_id}

    def delete_location(self, location_id: str, actor: str = "system") -> bool:
        """Only while nothing has been filed against it. A branch that has
        snapshots is part of the audit trail and is not removable."""
        used = self.con.execute(
            "SELECT COUNT(*) FROM snapshot WHERE location_id = ?", [location_id]
        ).fetchone()[0]
        if used:
            raise ValueError(
                f"{location_id} has {used} snapshots filed against it. "
                f"Removing it would break the audit trail."
            )
        self.con.execute("DELETE FROM location WHERE location_id = ?", [location_id])
        self.log("delete_location", "location", location_id, actor, {})
        self.con.commit()
        return True

    # ------------------------------------------------------------- aliases

    def aliases(self, target_type: str | None = None) -> List[dict]:
        sql = """SELECT alias_id, target_type, target_key, raw_value,
                        source_entity_id, confidence, resolved_by, resolved_at
                 FROM alias"""
        args: list = []
        if target_type:
            sql += " WHERE target_type = ?"
            args.append(target_type)
        sql += " ORDER BY target_type, raw_value"
        keys = ["alias_id", "target_type", "target_key", "raw_value",
                "source_entity_id", "confidence", "resolved_by", "resolved_at"]
        return [dict(zip(keys, _iso(r)))
                for r in self.con.execute(sql, args).fetchall()]

    def save_alias(
        self, target_type: str, raw_value: str, target_key: str,
        source_entity_id: str = "", actor: str = "user",
    ) -> dict:
        """Map one spelling onto one key. This is what turns a sanity finding
        about the same hospital under two codes into a decision."""
        if target_type not in ("item", "customer", "machine"):
            raise ValueError("target_type must be item, customer or machine")
        raw_value = raw_value.strip()
        target_key = target_key.strip()
        if not raw_value or not target_key:
            raise ValueError("An alias needs both a spelling and a target")
        if raw_value == target_key:
            raise ValueError("A value cannot be an alias of itself")

        self.con.execute(
            "DELETE FROM alias WHERE target_type = ? AND raw_value = ?",
            [target_type, raw_value],
        )
        alias_id = _uid()
        self.con.execute(
            "INSERT INTO alias VALUES (?, ?, ?, ?, ?, 1.0, ?, ?)",
            [alias_id, target_type, target_key, raw_value,
             source_entity_id or None, actor, _now()],
        )
        self.log("map_alias", "alias", alias_id, actor,
                 {"type": target_type, "from": raw_value, "to": target_key})
        self.con.commit()
        return {"alias_id": alias_id, "raw_value": raw_value,
                "target_key": target_key}

    def delete_alias(self, alias_id: str, actor: str = "user") -> bool:
        self.con.execute("DELETE FROM alias WHERE alias_id = ?", [alias_id])
        self.log("unmap_alias", "alias", alias_id, actor, {})
        self.con.commit()
        return True

    def alias_map(self, target_type: str) -> Dict[str, str]:
        """Spelling to canonical key, for the engine to apply on load."""
        rows = self.con.execute(
            """SELECT raw_value, target_key FROM alias
               WHERE target_type = ? AND target_key IS NOT NULL""",
            [target_type],
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def log(
        self,
        action: str,
        object_type: str = "",
        object_id: str = "",
        user_id: str = "system",
        detail: dict | None = None,
    ) -> str:
        aid = _uid()
        self.con.execute(
            "INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?, ?)",
            [aid, _now(), user_id, action, object_type, object_id,
             json.dumps(detail or {}, default=str)[:4000]],
        )
        return aid

    def audit_log(self, limit: int = 100) -> List[dict]:
        rows = self.con.execute(
            """SELECT at_utc, user_id, action, object_type, object_id, detail_json
               FROM audit ORDER BY at_utc DESC LIMIT ?""",
            [limit],
        ).fetchall()
        keys = ["at", "user_id", "action", "object_type", "object_id", "detail_json"]
        return [dict(zip(keys, r)) for r in rows]

    # ------------------------------------------------------------- stats

    def stats(self) -> dict:
        def one(sql: str) -> int:
            return self.con.execute(sql).fetchone()[0]

        return {
            "entities": one("SELECT COUNT(*) FROM entity"),
            "locations": one("SELECT COUNT(*) FROM location"),
            "items": one("SELECT COUNT(*) FROM item"),
            "snapshots": one("SELECT COUNT(*) FROM snapshot"),
            "live_snapshots": one("SELECT COUNT(*) FROM current_snapshot"),
            "inventory_facts": one("SELECT COUNT(*) FROM fact_inventory"),
            "demand_facts": one("SELECT COUNT(*) FROM fact_demand"),
            "metric_runs": one("SELECT COUNT(*) FROM metric_run"),
            "audit_entries": one("SELECT COUNT(*) FROM audit"),
        }


def _serialise(cls):
    """Take the store's lock for the whole of every public call.

    Done here rather than by hand so a method added later is covered without
    anybody remembering to. `tx` is left alone: it is a context manager, and
    wrapping it would hold the lock only while its generator was created
    rather than for the body of the `with`. Every caller of `tx` is a store
    method that already holds the lock.
    """
    for name, fn in list(vars(cls).items()):
        if name.startswith("_") or name == "tx":
            continue
        if not inspect.isfunction(fn):
            continue

        def guarded(self, *args, _fn=fn, **kwargs):
            with self._lock:
                return _fn(self, *args, **kwargs)

        guarded = functools.wraps(fn)(guarded)
        setattr(cls, name, guarded)
    return cls


_serialise(Store)
