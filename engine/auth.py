"""Authentication, roles and tenant context.

Three things live here and nowhere else:
  - password hashing and verification (Argon2id)
  - session tokens (the raw token never reaches the database)
  - what each role is allowed to see

Everything about WHICH ROWS a request can touch is enforced by Postgres
row-level security, not by this module. This module decides which FIELDS a
role may see, which the database cannot express as cleanly.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# Argon2id with deliberately non-trivial cost. The database file or dump is
# readable by anyone who gets it, so the hash has to be expensive to attack.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)

SESSION_HOURS = 12
SESSION_COOKIE = "sop_session"
MIN_PASSWORD_LENGTH = 10


class Role(str, Enum):
    OWNER = "owner"
    FINANCE = "finance"
    SUPPLY_CHAIN = "supply_chain"
    SERVICE = "service"
    VIEWER = "viewer"


# What each role may see. Absent from a role's set means the field is
# removed server-side before serialisation, not hidden in the browser.
# A webview can be inspected; a response that never contained the figure
# cannot be.
SENSITIVE_FIELDS = {
    "unit_cost", "stock_value", "excess_value", "order_value",
    "suggested_po_value", "cash_trapped",
}

ROLE_FIELDS: dict[Role, set[str]] = {
    Role.OWNER: SENSITIVE_FIELDS,
    Role.FINANCE: SENSITIVE_FIELDS,
    Role.SUPPLY_CHAIN: {"unit_cost", "stock_value", "excess_value",
                        "order_value", "suggested_po_value", "cash_trapped"},
    Role.SERVICE: set(),      # engineers see quantities, never money
    Role.VIEWER: {"stock_value"},
}

ROLE_MODULES: dict[Role, set[str]] = {
    Role.OWNER: {"dashboard", "stock", "upload", "history", "data",
                 "tenders", "placements", "receivables", "cash",
                 "service", "team", "settings"},
    Role.FINANCE: {"dashboard", "stock", "history", "receivables", "cash",
                   "tenders", "placements"},
    Role.SUPPLY_CHAIN: {"dashboard", "stock", "upload", "history",
                        "placements"},
    Role.SERVICE: {"dashboard", "service", "placements"},
    Role.VIEWER: {"dashboard", "stock"},
}

ROLE_CAN_UPLOAD = {Role.OWNER, Role.SUPPLY_CHAIN, Role.FINANCE}
ROLE_CAN_EXPORT = {Role.OWNER, Role.FINANCE}
ROLE_CAN_MANAGE_USERS = {Role.OWNER}


@dataclass(frozen=True)
class Principal:
    """Who is making this request, and on behalf of which organisation."""

    user_id: str
    org_id: str
    email: str
    display_name: str
    role: Role
    session_id: str

    @property
    def can_upload(self) -> bool:
        return self.role in ROLE_CAN_UPLOAD

    @property
    def can_export(self) -> bool:
        return self.role in ROLE_CAN_EXPORT

    @property
    def can_manage_users(self) -> bool:
        return self.role in ROLE_CAN_MANAGE_USERS

    def may_see(self, field: str) -> bool:
        if field not in SENSITIVE_FIELDS:
            return True
        return field in ROLE_FIELDS[self.role]

    def modules(self) -> set[str]:
        return ROLE_MODULES[self.role]


# ----------------------------------------------------------- passwords


class WeakPassword(ValueError):
    pass


def check_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(
            f"Use at least {MIN_PASSWORD_LENGTH} characters."
        )
    if password.lower() in {"password12", "1234567890", "qwertyuiop"}:
        raise WeakPassword("That password is too easy to guess.")


def hash_password(password: str) -> str:
    check_password_policy(password)
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except Exception:
        return False


# ------------------------------------------------------------ sessions


def new_session_token() -> str:
    """A fresh opaque token. Returned to the browser once, never stored."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Only the hash is stored, so a database dump yields no live sessions.

    SHA-256 is correct here and Argon2 would be wrong: the token already
    carries 256 bits of entropy, so there is nothing to brute force, and
    this runs on every request.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def session_expiry(hours: int = SESSION_HOURS) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


# ------------------------------------------------------- field masking


def mask(payload: dict, principal: Principal) -> dict:
    """Strip fields this role may not see, recursively.

    Removes the key rather than nulling it, so the client cannot tell a
    withheld figure from a zero and nothing sensitive crosses the wire.
    """
    if principal.role is Role.OWNER:
        return payload
    return _mask_value(payload, principal)


def _mask_value(value, principal: Principal):
    if isinstance(value, dict):
        return {
            k: _mask_value(v, principal)
            for k, v in value.items()
            if principal.may_see(k)
        }
    if isinstance(value, list):
        return [_mask_value(v, principal) for v in value]
    return value


def visible_modules(principal: Principal, module_ids: Iterable[str]) -> set[str]:
    allowed = principal.modules()
    return {m for m in module_ids if m in allowed}
