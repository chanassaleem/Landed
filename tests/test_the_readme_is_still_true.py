"""The README's factual claims, checked against the code.

It said "Phase 1 of five ... next is the importer screens on top of
it" for long enough that fourteen modules and a working importer
were built underneath the sentence. A reviewer reads this file
before anything else, and a stale one costs more credibility than
a missing one.

Only claims that can be checked mechanically are checked here. The
prose about why the product exists is not this file's business.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()
PLATFORM = (ROOT / "engine" / "platform.py").read_text()


def _module_names() -> set[str]:
    return set(re.findall(r'^\s+name="([^"]+)",', PLATFORM, re.M))


def test_the_readme_names_every_module_that_exists():
    """A module added without a line here is a module nobody reading
    the README knows about."""
    missing = sorted(n for n in _module_names() if n not in README)
    assert not missing, (
        f"these modules exist and the README does not mention them: "
        f"{missing}")


def test_the_readme_does_not_name_a_module_that_does_not_exist():
    """The other direction: a module renamed or removed leaves a
    promise behind."""
    # Only inside the Status list, so ordinary prose using a word like
    # "Cash" is not mistaken for a claim about a module.
    status = README[README.index("## Status"):]
    listed = re.search(r"modules are built and reachable: ([^.]+)\.",
                       status)
    assert listed, "the Status section no longer lists the modules"
    named = {n.strip() for n in re.split(r",| and ", listed.group(1))
             if n.strip()}
    unknown = sorted(named - _module_names())
    assert not unknown, f"the README promises modules that are gone: {unknown}"


def test_the_count_in_the_readme_matches_the_list_in_the_readme():
    status = README[README.index("## Status"):]
    said = re.search(r"\b(\w+) modules are built", status)
    assert said, "the Status section no longer counts the modules"
    words = {"Twelve": 12, "Thirteen": 13, "Fourteen": 14,
             "Fifteen": 15, "Sixteen": 16, "Seventeen": 17,
             "Eighteen": 18, "Nineteen": 19, "Twenty": 20}
    n = words.get(said.group(1))
    assert n is not None, (
        f"{said.group(1)!r} is not a number this test knows; add it")
    assert n == len(_module_names()), (
        f"the README says {said.group(1)} ({n}) modules, "
        f"engine/platform.py defines {len(_module_names())}")


def test_every_command_the_readme_tells_you_to_run_exists():
    """A tool renamed leaves an instruction that fails on the first
    try somebody gives it."""
    for script in set(re.findall(r"python (tools/[\w./]+\.py)", README)):
        assert (ROOT / script).exists(), (
            f"the README tells you to run {script}, which is not there")


def test_the_readme_does_not_still_call_this_phase_one():
    """It did, for months after the phase ended."""
    status = README[README.index("## Status"):]
    assert "Phase 1" not in status, (
        "the Status section still says Phase 1; it has not been true "
        "since the module screens shipped")
