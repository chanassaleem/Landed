"""The connection to Supabase, and the proof that its policies hold.

Two things live here and they are deliberately not separable.

The first is a connection. Nothing exotic: a pooled psycopg connection to
the project's Postgres, opened once and reused, because a fresh TLS
handshake to Sydney on every request costs more than the query does.

The second is the reason this module exists rather than a line of
`psycopg.connect` in a route. Supabase's row-level security protects data
only when a request actually runs AS somebody. A connection made with the
database password is a superuser connection: every policy in
supabase/migrations is bypassed, silently, and the code reads exactly the
same as the safe version. So the only ways to get a connection out of
this module are `as_user`, which sets the role and the claims the
policies read, and `as_service`, which does not and says so in its name.

`check()` then proves the arrangement is live. It asks, as a user who
belongs to no organisation, for rows from a protected table, and expects
none. That is not a connectivity test with extra steps -- a reachable
database with RLS switched off passes every ordinary health check ever
written, and fails this one.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

try:  # The app runs without these; only the Supabase paths need them.
    import psycopg
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover - exercised by the import guard test
    psycopg = None
    ConnectionPool = None


class NotConfigured(RuntimeError):
    """No database is configured. Raised rather than guessed at."""


# A password in a DSN is the one thing that must never reach a log line,
# an error page or an exception a framework renders in a traceback.
_PASSWORD = re.compile(r"(?<=:)[^:@/]+(?=@)")


def redact(dsn: str) -> str:
    """A DSN safe to print. Everything but the password survives."""
    return _PASSWORD.sub("********", dsn or "")


@dataclass(frozen=True)
class Config:
    """Where the database is, and the keys the browser is allowed to see.

    Note what is absent: the service role key. It is read at the point of
    use in `as_service`, never held on an object that might get logged,
    serialised into a debug endpoint, or printed by an exception handler.
    """

    dsn: str
    project_url: str = ""
    anon_key: str = ""

    @classmethod
    def from_env(cls, env=None) -> "Config | None":
        env = os.environ if env is None else env
        dsn = env.get("DATABASE_URL") or env.get("LANDED_TEST_DSN") or ""
        if not dsn:
            return None
        return cls(
            dsn=dsn,
            project_url=env.get("SUPABASE_URL", ""),
            anon_key=env.get("SUPABASE_ANON_KEY", ""),
        )

    def __repr__(self) -> str:  # never leak the password
        return f"Config(dsn={redact(self.dsn)!r}, project_url={self.project_url!r})"

    @property
    def warnings(self) -> list[str]:
        """Configuration that will work but is probably a mistake."""
        out = []
        # Supabase's direct connection resolves to IPv6 only unless the
        # project pays for the IPv4 add-on. Render, and most other hosts,
        # are IPv4-only, so the direct string fails with a name-resolution
        # error that looks nothing like its cause.
        if ".supabase.co" in self.dsn and "pooler.supabase.com" not in self.dsn:
            out.append(
                "DATABASE_URL looks like the direct connection string "
                "(db.<ref>.supabase.co), which is IPv6-only without the paid "
                "IPv4 add-on. On an IPv4-only host use the Session pooler "
                "string from Connect in the dashboard."
            )
        if "pooler.supabase.com:6543" in self.dsn:
            out.append(
                "DATABASE_URL uses the transaction pooler (port 6543), which "
                "does not keep session state. This app sets the role and the "
                "JWT claims per transaction, so use the Session pooler "
                "(port 5432) instead."
            )
        if self.dsn.startswith("postgres") and "sslmode" not in self.dsn:
            out.append(
                "DATABASE_URL does not set sslmode. Supabase requires TLS, "
                "and being explicit stops a misconfigured client from "
                "quietly falling back."
            )
        return out


@dataclass(frozen=True)
class Health:
    reachable: bool
    rls_holding: bool | None
    latency_ms: int | None
    detail: str

    @property
    def ok(self) -> bool:
        return self.reachable and self.rls_holding is True

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reachable": self.reachable,
            "rls_holding": self.rls_holding,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
        }


class Database:
    """A pooled connection that will only hand out scoped sessions."""

    def __init__(
        self,
        config: Config,
        min_size: int = 0,
        max_size: int = 4,
        connect_timeout: float = 5.0,
    ):
        if psycopg is None or ConnectionPool is None:
            raise NotConfigured("psycopg and psycopg_pool are not installed")
        self.config = config
        # Every timeout here is short on purpose. A health endpoint that
        # hangs for a minute before admitting the database is unreachable
        # is worse than no health endpoint: the platform's own check times
        # out first, so the service is marked unhealthy with no reason
        # attached. Fail in seconds and say why.
        #
        # min_size=0 and open=False together mean construction never
        # blocks a cold start on a database that is slow or asleep --
        # which, on a free tier that spins down, is most mornings.
        self._pool = ConnectionPool(
            config.dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            timeout=connect_timeout,          # waiting for a pooled connection
            reconnect_timeout=connect_timeout,  # giving up on a dead database
            kwargs={"autocommit": True, "connect_timeout": connect_timeout},
        )
        self._opened = False

    def open(self) -> None:
        if not self._opened:
            self._pool.open()
            self._opened = True

    def close(self) -> None:
        if self._opened:
            self._pool.close()
            self._opened = False

    def __repr__(self) -> str:
        return f"Database({self.config!r})"

    # ------------------------------------------------------------ sessions

    @contextmanager
    def as_user(self, user_id: str | uuid.UUID) -> Iterator["psycopg.Connection"]:
        """A transaction acting as that signed-in user.

        Every policy in supabase/migrations is in force here. The role and
        the claims are set with SET LOCAL inside the transaction, so they
        cannot outlive it and leak into whatever uses the pooled
        connection next -- which, on a pool, would be another customer's
        request.
        """
        self.open()
        with self._pool.connection() as con:
            with con.transaction():
                con.execute("set local role authenticated")
                con.execute(
                    "select set_config('request.jwt.claims', %s, true)",
                    (json.dumps({"sub": str(user_id), "role": "authenticated"}),),
                )
                yield con

    @contextmanager
    def as_service(self) -> Iterator["psycopg.Connection"]:
        """A transaction with NO row-level security.

        Every tenant's rows are visible and writable. This exists for
        migrations, billing writes to entitlements, and scheduled jobs
        that are not acting for any one customer. If a request handler
        reaches for this, the handler is wrong: it should know which user
        it is serving, and `as_user` is how it says so.
        """
        self.open()
        with self._pool.connection() as con:
            with con.transaction():
                yield con

    # -------------------------------------------------------------- health

    def check(self, probe_table: str = "items") -> Health:
        """Reachable, and are the policies actually on?

        The second half is the point. A database with RLS disabled answers
        `select 1` perfectly happily, so a plain connectivity check reports
        green on a project that is serving every tenant's rows to anyone
        who asks. This one asks as a user who is a member of nothing and
        expects to be told nothing.
        """
        started = time.monotonic()
        try:
            self.open()
            nobody = uuid.uuid4()   # a real UUID belonging to no account
            with self.as_user(nobody) as con:
                rows = con.execute(
                    f"select count(*) from public.{probe_table}"
                ).fetchone()[0]
            latency = int((time.monotonic() - started) * 1000)
        except Exception as exc:  # noqa: BLE001 - reported, never raised at a user
            # The message can carry the DSN, so it is redacted before it
            # goes anywhere a person or a log aggregator might see it.
            return Health(
                reachable=False, rls_holding=None, latency_ms=None,
                detail=redact(f"{type(exc).__name__}: {exc}")[:300],
            )

        if rows:
            return Health(
                reachable=True, rls_holding=False, latency_ms=latency,
                detail=(
                    f"A user who belongs to no organisation can read "
                    f"{rows} row(s) from {probe_table}. Row-level security "
                    f"is not protecting this table."
                ),
            )
        return Health(
            reachable=True, rls_holding=True, latency_ms=latency,
            detail="Connected, and a user with no membership sees nothing.",
        )


_database: Database | None = None


def database() -> Database:
    """The process-wide connection, built on first use.

    Built lazily rather than at import so that the app still starts, and
    still serves its health endpoint, when the database is unreachable --
    which is exactly when you most want to be able to reach the app and
    read the reason.
    """
    global _database
    if _database is None:
        config = Config.from_env()
        if config is None:
            raise NotConfigured(
                "DATABASE_URL is not set. In Render this is the Session "
                "pooler connection string from the Supabase dashboard."
            )
        _database = Database(config)
    return _database


def reset() -> None:
    """Drop the process-wide connection. For tests."""
    global _database
    if _database is not None:
        _database.close()
    _database = None
