"""Postgres store with tenant context set per transaction.

Every data-touching call runs inside `self.tenant(org_id)`, which issues
SET LOCAL app.org_id before anything else. Because it is LOCAL, the value
dies with the transaction, so a pooled connection cannot carry one
organisation's context into the next request. That property is what makes
connection pooling safe here, and it is tested.

This store never calculates. It reads and writes.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from engine import __version__
from engine.auth import (
    Principal,
    Role,
    hash_password,
    hash_token,
    new_session_token,
    session_expiry,
    verify_password,
)
from engine.schema import (
    DemandPeriod,
    InventoryResult,
    ItemSnapshot,
    Thresholds,
)

SCHEMA = Path(__file__).parent / "pg_schema.sql"
RLS = Path(__file__).parent / "pg_rls.sql"


def _uid() -> str:
    return str(uuid.uuid4())


class AuthError(Exception):
    """Login failed. Deliberately says nothing about which part failed."""


APP_ROLE = "sop_app"


class PgStore:
    """
    Two connection identities, deliberately separated:

      admin_dsn  owns the schema and the SECURITY DEFINER functions. Used
                 by migrations only. Never serves a request.
      dsn        the application role. Not the owner, so every RLS policy
                 applies to it with no exceptions.

    Collapsing these into one role is what makes RLS decorative, because a
    table's owner is exempt from its own policies unless FORCE is set, and
    FORCE then blocks the signup and login paths.
    """

    def __init__(self, dsn: str, admin_dsn: str | None = None):
        self.dsn = dsn
        self.admin_dsn = admin_dsn or dsn

    @contextmanager
    def _conn(self):
        with psycopg.connect(self.dsn, row_factory=dict_row) as con:
            yield con

    @contextmanager
    def tenant(self, org_id: str):
        """Open a transaction scoped to one organisation.

        SET LOCAL, not SET: the setting is reverted when the transaction
        ends, so the connection returns to the pool with no tenant context.
        """
        with psycopg.connect(self.dsn, row_factory=dict_row) as con:
            with con.transaction():
                con.execute("SELECT set_config('app.org_id', %s, true)",
                            (str(org_id),))
                yield con

    # ------------------------------------------------------ provisioning

    def install(self, app_password: str | None = None) -> None:
        """Create the schema, the application role and the RLS policies.

        Runs as the owner. Safe to run repeatedly.
        """
        with psycopg.connect(self.admin_dsn, row_factory=dict_row) as con:
            con.execute(SCHEMA.read_text())
            # CREATE TABLE IF NOT EXISTS leaves a database installed before a
            # column was added on its old columns, so additions are applied
            # here as well. ADD COLUMN IF NOT EXISTS makes this idempotent,
            # which the repeated-install promise above depends on.
            for table, column, ddl in (
                ("entity", "configured", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("location", "configured", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ):
                con.execute(
                    sql.SQL("ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {c} " + ddl)
                    .format(t=sql.Identifier(table), c=sql.Identifier(column))
                )
            con.execute(RLS.read_text())

            exists = con.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,)
            ).fetchone()

            # Postgres does not accept query parameters in DDL, so the
            # password cannot be bound with %s here; that fails with
            # 'syntax error at or near "$1"'. psycopg.sql composes the
            # statement with the value quoted by the driver instead, which is
            # safe and is the only way to write this.
            role = sql.Identifier(APP_ROLE)
            if not exists and app_password:
                con.execute(
                    sql.SQL(
                        "CREATE ROLE {role} LOGIN PASSWORD {password} "
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
                    ).format(role=role, password=sql.Literal(app_password))
                )
            if exists or app_password:
                # DML only. No ownership, no DDL, so RLS always applies.
                con.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA public TO {role}").format(role=role)
                )
                con.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE "
                        "ON ALL TABLES IN SCHEMA public TO {role}"
                    ).format(role=role)
                )
                con.execute(
                    sql.SQL(
                        "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {role}"
                    ).format(role=role)
                )
                con.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
                    ).format(role=role)
                )
            con.commit()

    def create_org(self, name: str, country: str = "PK") -> str:
        """Signup. Goes through the privileged path because no org context
        can exist before the org does. Rate limit this at the edge."""
        org_id = _uid()
        with self._conn() as con:
            con.execute("SELECT provision_org(%s,%s,%s)", (org_id, name, country))
            con.commit()
        return org_id

    def create_user(
        self, org_id: str, email: str, display_name: str,
        password: str, role: Role = Role.VIEWER,
        actor: Optional[Principal] = None,
    ) -> str:
        """Create a user inside an org.

        When an actor is given, they must be an owner of that same org.
        That check cannot live in RLS, because the row being authorised
        does not exist yet, so it is enforced here and tested.
        """
        if actor is not None:
            if actor.org_id != org_id:
                raise PermissionError("Cannot create a user in another organisation.")
            if not actor.can_manage_users:
                raise PermissionError(f"{actor.role.value} cannot create users.")

        user_id = _uid()
        pw = hash_password(password)
        with self._conn() as con:
            con.execute(
                "SELECT provision_user(%s,%s,%s,%s,%s,%s)",
                (user_id, org_id, email.lower(), display_name, pw, role.value),
            )
            con.commit()
        return user_id

    # ------------------------------------------------------------- auth

    def login(
        self, email: str, password: str,
        user_agent: str = "", ip: str = "",
    ) -> tuple[str, Principal]:
        """Return (raw token, principal) or raise AuthError.

        The same error is raised for an unknown email, a wrong password and
        a locked account, so the endpoint cannot be used to discover which
        addresses have accounts.
        """
        with self._conn() as con:
            row = con.execute("SELECT * FROM auth_lookup(%s)", (email,)).fetchone()

            if row is None:
                # Spend comparable time so timing does not reveal the miss.
                verify_password(
                    "$argon2id$v=19$m=65536,t=3,p=2$"
                    "c29tZXNhbHRzb21lc2FsdA$0000000000000000000000000000000",
                    password,
                )
                raise AuthError("Email or password is wrong.")

            if row["locked_until"] and row["locked_until"] > datetime.now(timezone.utc):
                raise AuthError("Email or password is wrong.")
            if not row["is_active"]:
                raise AuthError("Email or password is wrong.")

            if not verify_password(row["password_hash"], password):
                con.execute("SELECT auth_record_failure(%s)", (email,))
                con.commit()
                raise AuthError("Email or password is wrong.")

            token = new_session_token()
            session_id = _uid()
            con.execute(
                "SELECT auth_create_session(%s,%s,%s,%s,%s,%s,%s)",
                (session_id, hash_token(token), row["user_id"], row["org_id"],
                 session_expiry(), user_agent[:300], ip[:64]),
            )
            con.commit()

        principal = Principal(
            user_id=str(row["user_id"]), org_id=str(row["org_id"]),
            email=email.lower(), display_name=row["display_name"],
            role=Role(row["role"]), session_id=session_id,
        )
        self.log(principal, "login", "user", principal.user_id)
        return token, principal

    def resolve(self, token: str) -> Optional[Principal]:
        if not token:
            return None
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM auth_session(%s)", (hash_token(token),)
            ).fetchone()
        if row is None:
            return None
        return Principal(
            user_id=str(row["user_id"]), org_id=str(row["org_id"]),
            email=row["email"], display_name=row["display_name"],
            role=Role(row["role"]), session_id=str(row["session_id"]),
        )

    def logout(self, token: str) -> None:
        with self._conn() as con:
            con.execute("SELECT auth_revoke(%s)", (hash_token(token),))
            con.commit()

    # ------------------------------------------------------- tenant data

    def ensure_entity(self, p: Principal, name: str = "Main") -> str:
        with self.tenant(p.org_id) as con:
            row = con.execute(
                "SELECT entity_id FROM entity WHERE legal_name = %s", (name,)
            ).fetchone()
            if row:
                return str(row["entity_id"])
            eid = _uid()
            con.execute(
                "INSERT INTO entity (entity_id, org_id, legal_name) VALUES (%s,%s,%s)",
                (eid, p.org_id, name),
            )
            return eid

    def ensure_location(self, p: Principal, entity_id: str, name: str = "Main") -> str:
        with self.tenant(p.org_id) as con:
            row = con.execute(
                "SELECT location_id FROM location WHERE entity_id = %s AND name = %s",
                (entity_id, name),
            ).fetchone()
            if row:
                return str(row["location_id"])
            lid = _uid()
            con.execute(
                """INSERT INTO location (location_id, org_id, entity_id, name)
                   VALUES (%s,%s,%s,%s)""",
                (lid, p.org_id, entity_id, name),
            )
            return lid

    def upsert_items(self, p: Principal, items: Iterable[ItemSnapshot]) -> int:
        rows = [
            (p.org_id, i.item_key, i.description, i.category,
             i.is_clinical_critical)
            for i in items
        ]
        with self.tenant(p.org_id) as con:
            con.cursor().executemany(
                """INSERT INTO item
                     (org_id, item_key, description, category, is_clinical_critical)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (org_id, item_key) DO UPDATE SET
                     description = EXCLUDED.description,
                     category = EXCLUDED.category,
                     is_clinical_critical = EXCLUDED.is_clinical_critical""",
                rows,
            )
        return len(rows)

    def add_snapshot(
        self, p: Principal, *, report_type: str, as_of: date,
        entity_id: str, location_id: str, source_filename: str = "",
        source_sha256: str = "", row_count: int = 0, rows_rejected: int = 0,
        status: str = "valid",
    ) -> str:
        snapshot_id = _uid()
        with self.tenant(p.org_id) as con:
            con.execute(
                """UPDATE snapshot SET superseded_by = %s
                   WHERE entity_id = %s AND location_id = %s
                     AND report_type = %s AND as_of_date = %s
                     AND superseded_by IS NULL""",
                (snapshot_id, entity_id, location_id, report_type, as_of),
            )
            con.execute(
                """INSERT INTO snapshot
                   (snapshot_id, org_id, entity_id, location_id, report_type,
                    as_of_date, uploaded_by, source_filename, source_sha256,
                    row_count, rows_rejected, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (snapshot_id, p.org_id, entity_id, location_id, report_type,
                 as_of, p.user_id, source_filename, source_sha256,
                 row_count, rows_rejected, status),
            )
        self.log(p, "upload", "snapshot", snapshot_id,
                 {"report_type": report_type, "rows": row_count})
        return snapshot_id

    def add_inventory_facts(
        self, p: Principal, snapshot_id: str, items: Sequence[ItemSnapshot]
    ) -> int:
        with self.tenant(p.org_id) as con:
            con.cursor().executemany(
                """INSERT INTO fact_inventory
                   (snapshot_id, org_id, item_key, on_hand, open_po, unit_cost,
                    lead_time_days, lead_time_days_std, expiry_date)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(snapshot_id, p.org_id, i.item_key, i.on_hand, i.open_po,
                  i.unit_cost, i.lead_time_days, i.lead_time_days_std,
                  i.expiry_date) for i in items],
            )
        return len(items)

    def add_demand_facts(
        self, p: Principal, snapshot_id: str, periods: Sequence[DemandPeriod]
    ) -> int:
        with self.tenant(p.org_id) as con:
            con.cursor().executemany(
                """INSERT INTO fact_demand
                   (snapshot_id, org_id, item_key, period, quantity)
                   VALUES (%s,%s,%s,%s,%s)""",
                [(snapshot_id, p.org_id, d.item_key, d.period, d.quantity)
                 for d in periods],
            )
        return len(periods)

    def read_items(self, p: Principal, snapshot_id: str) -> List[ItemSnapshot]:
        with self.tenant(p.org_id) as con:
            rows = con.execute(
                """SELECT f.item_key, COALESCE(i.description,'') AS description,
                          COALESCE(i.category,'') AS category, f.on_hand,
                          f.open_po, f.unit_cost, f.lead_time_days,
                          f.lead_time_days_std,
                          COALESCE(i.is_clinical_critical,false) AS crit,
                          f.expiry_date
                   FROM fact_inventory f
                   LEFT JOIN item i
                     ON i.item_key = f.item_key AND i.org_id = f.org_id
                   WHERE f.snapshot_id = %s ORDER BY f.item_key""",
                (snapshot_id,),
            ).fetchall()
        return [
            ItemSnapshot(
                item_key=r["item_key"], description=r["description"],
                category=r["category"], on_hand=r["on_hand"],
                open_po=r["open_po"], unit_cost=r["unit_cost"],
                lead_time_days=r["lead_time_days"],
                lead_time_days_std=r["lead_time_days_std"],
                is_clinical_critical=r["crit"], expiry_date=r["expiry_date"],
            )
            for r in rows
        ]

    def read_demand(self, p: Principal, snapshot_id: str) -> List[DemandPeriod]:
        with self.tenant(p.org_id) as con:
            rows = con.execute(
                """SELECT item_key, period, quantity FROM fact_demand
                   WHERE snapshot_id = %s ORDER BY item_key, period""",
                (snapshot_id,),
            ).fetchall()
        return [
            DemandPeriod(item_key=r["item_key"], period=r["period"],
                         quantity=r["quantity"])
            for r in rows
        ]

    def latest_snapshot_id(
        self, p: Principal, report_type: str,
        entity_id: Optional[str] = None, location_id: Optional[str] = None,
    ) -> Optional[str]:
        sql = """SELECT snapshot_id FROM snapshot
                 WHERE report_type = %s AND superseded_by IS NULL
                   AND status <> 'rejected'"""
        args: list = [report_type]
        if entity_id:
            sql += " AND entity_id = %s"; args.append(entity_id)
        if location_id:
            sql += " AND location_id = %s"; args.append(location_id)
        sql += " ORDER BY as_of_date DESC, uploaded_at DESC LIMIT 1"
        with self.tenant(p.org_id) as con:
            row = con.execute(sql, args).fetchone()
        return str(row["snapshot_id"]) if row else None

    def snapshots(self, p: Principal, limit: int = 40) -> List[dict]:
        with self.tenant(p.org_id) as con:
            return con.execute(
                """SELECT snapshot_id, entity_id, location_id, report_type,
                          as_of_date, uploaded_at, source_filename, row_count,
                          rows_rejected, status, superseded_by
                   FROM snapshot ORDER BY uploaded_at DESC LIMIT %s""",
                (limit,),
            ).fetchall()

    # ------------------------------------------------------ metric runs

    def save_thresholds(self, p: Principal, thresholds: Thresholds) -> str:
        payload = asdict(thresholds)
        payload["service_targets"] = {
            f"{k[0]}|{k[1]}": v for k, v in thresholds.service_targets.items()
        }
        blob = json.dumps(payload, sort_keys=True)
        with self.tenant(p.org_id) as con:
            row = con.execute(
                "SELECT threshold_set_id FROM threshold_set WHERE payload = %s::jsonb",
                (blob,),
            ).fetchone()
            if row:
                return str(row["threshold_set_id"])
            tsid = _uid()
            con.execute(
                """INSERT INTO threshold_set
                   (threshold_set_id, org_id, created_by, payload)
                   VALUES (%s,%s,%s,%s::jsonb)""",
                (tsid, p.org_id, p.user_id, blob),
            )
        return tsid

    def save_metric_run(
        self, p: Principal, results: Sequence[InventoryResult], *,
        as_of: date, threshold_set_id: str, snapshot_ids: Sequence[str],
        order_values: dict, entity_id: str, location_id: str,
    ) -> str:
        run_id = _uid()
        with self.tenant(p.org_id) as con:
            con.execute(
                """INSERT INTO metric_run
                   (run_id, org_id, entity_id, location_id, as_of_date,
                    created_by, threshold_set_id, snapshot_ids,
                    engine_version, item_count)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)""",
                (run_id, p.org_id, entity_id, location_id, as_of, p.user_id,
                 threshold_set_id, json.dumps(list(snapshot_ids)),
                 __version__, len(results)),
            )
            con.cursor().executemany(
                """INSERT INTO metric_result
                   (run_id, org_id, item_key, demand_class, abc_class,
                    service_target, service_metric, safety_stock,
                    reorder_point, order_up_to, suggested_order_qty,
                    order_value, months_cover, status, stock_value,
                    excess_value, days_to_expiry, reason)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(run_id, p.org_id, r.item_key, r.demand_class.value,
                  r.abc_class.value, r.service_target, r.service_metric.value,
                  r.safety_stock, r.reorder_point, r.order_up_to,
                  r.suggested_order_qty, order_values.get(r.item_key, 0.0),
                  r.months_cover, r.status.value, r.stock_value,
                  r.excess_value, r.days_to_expiry, r.reason)
                 for r in results],
            )
        return run_id

    def runs(self, p: Principal, limit: int = 30) -> List[dict]:
        with self.tenant(p.org_id) as con:
            return con.execute(
                """SELECT r.run_id, r.as_of_date, r.created_at, r.item_count,
                          r.engine_version,
                          SUM(m.stock_value) AS stock_value,
                          SUM(m.order_value) AS order_value,
                          SUM(m.excess_value) AS excess_value,
                          COUNT(*) FILTER (WHERE m.status='CRITICAL') AS critical
                   FROM metric_run r JOIN metric_result m ON m.run_id = r.run_id
                   GROUP BY r.run_id, r.as_of_date, r.created_at, r.item_count,
                            r.engine_version
                   ORDER BY r.created_at DESC LIMIT %s""",
                (limit,),
            ).fetchall()

    # ------------------------------------------------------------ audit

    def log(
        self, p: Principal, action: str, object_type: str = "",
        object_id: str = "", detail: dict | None = None,
    ) -> None:
        with self.tenant(p.org_id) as con:
            con.execute(
                """INSERT INTO audit
                   (audit_id, org_id, user_id, action, object_type, object_id, detail)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (_uid(), p.org_id, p.user_id, action, object_type,
                 str(object_id), json.dumps(detail or {}, default=str)),
            )

    def audit_log(self, p: Principal, limit: int = 100) -> List[dict]:
        with self.tenant(p.org_id) as con:
            return con.execute(
                """SELECT a.at_utc, a.action, a.object_type, a.object_id,
                          a.detail, COALESCE(u.display_name,'system') AS who
                   FROM audit a LEFT JOIN app_user u ON u.user_id = a.user_id
                   ORDER BY a.at_utc DESC LIMIT %s""",
                (limit,),
            ).fetchall()

    def stats(self, p: Principal) -> dict:
        with self.tenant(p.org_id) as con:
            row = con.execute(
                """SELECT
                     (SELECT COUNT(*) FROM entity) AS entities,
                     (SELECT COUNT(*) FROM location) AS locations,
                     (SELECT COUNT(*) FROM item) AS items,
                     (SELECT COUNT(*) FROM snapshot) AS snapshots,
                     (SELECT COUNT(*) FROM snapshot WHERE superseded_by IS NULL)
                       AS live_snapshots,
                     (SELECT COUNT(*) FROM fact_inventory) AS inventory_facts,
                     (SELECT COUNT(*) FROM fact_demand) AS demand_facts,
                     (SELECT COUNT(*) FROM metric_run) AS metric_runs,
                     (SELECT COUNT(*) FROM audit) AS audit_entries,
                     (SELECT COUNT(*) FROM app_user) AS users"""
            ).fetchone()
        return dict(row)
