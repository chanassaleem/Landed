"""location_id means one thing everywhere, or it means nothing.

Two readers in engine/db/snapshot.py emitted the raw uuid of a branch
where every other reader emits its code -- read_employees and
read_opportunities, both stringifying the column straight off the
row. Neither showed a branch on screen, so both were invisible; and
both modules are group-level, so no branch filter was narrowing them.

That is exactly what makes it worth a test rather than a fix. The
first per-branch payroll report, or the first pipeline split by
branch, would have matched nothing and looked like an empty month --
with the data present, the query correct, and the join silently
comparing a uuid to 'KHI'.

Read from the source rather than from a database, so it covers every
reader including those whose module nobody has seeded.
"""

from __future__ import annotations

import pathlib
import re

SNAPSHOT = (pathlib.Path(__file__).resolve().parents[1]
            / "engine" / "db" / "snapshot.py").read_text()


def test_no_reader_passes_a_branch_uuid_off_as_a_branch_code():
    """str() around a location column is the shape of the mistake:
    it turns a uuid into a string that looks like data and compares
    equal to nothing anybody will ever filter on."""
    bad = [
        line.strip()
        for line in SNAPSHOT.split("\n")
        if re.search(r"location_id\s*=\s*str\(", line)
    ]
    assert not bad, (
        "a reader is stringifying a branch uuid instead of joining "
        "public.locations for its code:\n  " + "\n  ".join(bad))


def test_every_reader_that_selects_a_branch_joins_for_its_code():
    """A query naming o.location_id or e.location_id in its select
    list, with no join to public.locations, is returning the uuid."""
    missing = []
    for name, body in _readers().items():
        selects_raw = re.search(r'"select[^"]*\b\w+\.location_id\b', body)
        if selects_raw and "join public.locations" not in body:
            missing.append(name)
    assert not missing, (
        "these readers select a branch column without joining for its "
        f"code: {missing}")


def _readers() -> dict[str, str]:
    lines = SNAPSHOT.split("\n")
    starts = [(i, l.split("(")[0][4:])
              for i, l in enumerate(lines) if l.startswith("def ")]
    out = {}
    for n, (i, name) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        out[name] = "\n".join(lines[i:end])
    return out


def test_the_readers_are_actually_being_found():
    """A source-reading test that matches nothing passes for ever.
    read_machines is in this file and always will be."""
    found = _readers()
    assert "read_machines" in found
    assert "join public.locations" in found["read_employees"]
