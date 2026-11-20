"""Narrowing the facts to one company or one branch.

Scope was a client-side row filter to begin with, which could never be right.
Filtering rows in the page narrows the list but not the figures above it, so
picking Karachi left the tiles reading the whole group: the table and the
number above it disagreed, and the number is the one people quote. Worse,
some figures cannot be filtered at all after the fact. Safety stock is
computed from a location's own demand history, days-sales-outstanding is a
ratio against revenue, and forecast error is measured per line -- none of
them can be recovered by adding up rows that have already been through the
engine.

So scope belongs here, before the modules run: narrow the facts, then compute.
Every figure downstream is then internally consistent, because it was computed
on exactly the rows the screen is showing.

Two modules are deliberately left out of that, because narrowing their facts
would not make them more accurate, it would make them wrong:

  Cash. One bank balance serves the group. Running thirteen weeks for one
  branch against the group's opening balance would invent a balance that
  branch does not have, and splitting the balance between branches is a
  treasury decision nobody has made.

  Transfers. A transfer has a sender and a receiver. Filter the facts to one
  branch and the module can no longer see the other side of the move it
  exists to suggest.

Both are computed on the whole group and flagged, so the screen can say so
rather than imply a figure it has not got.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence, TypeVar

T = TypeVar("T")

ALL = "all"


@dataclass(frozen=True)
class Scope:
    """Which company and which branch, or neither."""

    entity: str | None = None
    location: str | None = None

    @property
    def active(self) -> bool:
        return bool(self.entity or self.location)

    def label(self, entity_name=None, location_name=None) -> str:
        """For a screen to show what it is looking at, in words."""
        if self.location:
            place = location_name or self.location
            who = entity_name or self.entity
            return f"{place}, {who}" if who else str(place)
        if self.entity:
            return str(entity_name or self.entity)
        return "the whole group"

    def matches(self, record) -> bool:
        if self.entity and getattr(record, "entity_id", None) != self.entity:
            return False
        if self.location and getattr(record, "location_id", None) != self.location:
            return False
        return True


def read(entity: str | None, location: str | None) -> Scope:
    """Build a Scope from form values, where 'all' and '' both mean no filter.

    A branch chosen without a company is kept as a branch filter on its own:
    branch codes are unique in this model, so it is unambiguous, and refusing
    it would make a shareable link fragile for no benefit.
    """
    def clean(v):
        v = (v or "").strip()
        return None if v in ("", ALL) else v

    return Scope(entity=clean(entity), location=clean(location))


def filter_records(records: Sequence[T], scope: Scope) -> list[T]:
    if not scope.active:
        return list(records)
    return [r for r in records if scope.matches(r)]


def touches(record, scope: Scope, sides: Iterable[tuple[str, str]]) -> bool:
    """True when any of the given (entity field, location field) pairs match.

    For records with two ends, such as a transfer suggestion, so that a branch
    sees moves both into and out of it rather than only the ones it sends.
    """
    if not scope.active:
        return True
    for entity_field, location_field in sides:
        ok = True
        if scope.entity and getattr(record, entity_field, None) != scope.entity:
            ok = False
        if ok and scope.location and getattr(record, location_field, None) != scope.location:
            ok = False
        if ok:
            return True
    return False


# Some facts have no branch to narrow to. A trial balance belongs to a
# company: there is no Karachi balance sheet unless somebody keeps one, and
# this product is not going to invent one by filtering rows. These are
# narrowed by company and left alone by the branch selector.
ENTITY_ONLY = ("accounts", "marketing_spend")


def apply(
    scope: Scope,
    *,
    items: Sequence = (),
    demand: Sequence = (),
    invoices: Sequence = (),
    jobs: Sequence = (),
    placements: Sequence = (),
    commitments: Sequence = (),
    accounts: Sequence = (),
    employees: Sequence = (),
    opportunities: Sequence = (),
    marketing_spend: Sequence = (),
) -> dict:
    """Narrow every fact collection, and report what was set aside.

    The counts are for the screen: "148 of 420 lines" is the difference
    between a filter that worked and a filter that found nothing.
    """
    given = {
        "items": items,
        "demand": demand,
        "invoices": invoices,
        "jobs": jobs,
        "placements": placements,
        "commitments": commitments,
        "accounts": accounts,
        "employees": employees,
        "opportunities": opportunities,
        "marketing_spend": marketing_spend,
    }
    by_company = Scope(entity=scope.entity)
    kept = {
        k: filter_records(v, by_company if k in ENTITY_ONLY else scope)
        for k, v in given.items()
    }
    counts = {
        k: {"kept": len(kept[k]), "total": len(given[k])} for k in given
    }
    return {"records": kept, "counts": counts}
