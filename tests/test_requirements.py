"""Everything the shipped code imports is declared in requirements.txt.

This exists because of a deploy that built cleanly and then died on
startup with ModuleNotFoundError: no module named 'httpx'. The import
was fine locally -- httpx was already in the development environment --
and the whole test suite passed, because the test suite runs in that
same environment. Nothing anywhere expressed the difference between
"installed on this machine" and "installed where it runs".

So this walks the source, collects every top-level module imported, and
subtracts the standard library and the project's own packages. What is
left must appear in requirements.txt. It is a cheap test that catches a
class of failure the rest of the suite is structurally blind to.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Shipped: what the Dockerfile copies. Not tests, not tooling scripts --
# a test-only dependency has no business in a production image.
SHIPPED = ("engine", "web", "tools")

OWN = {"engine", "web", "tools", "tests"}

# Distribution names differ from import names often enough to need a map
# rather than a guess.
DIST = {
    "jwt": "pyjwt",
    "psycopg_pool": "psycopg-pool",
    "argon2": "argon2-cffi",
    "dateutil": "python-dateutil",
    "yaml": "pyyaml",
    "multipart": "python-multipart",
}


def imported_modules() -> dict[str, set[str]]:
    """Top-level module name -> the files that import it."""
    found: dict[str, set[str]] = {}
    for package in SHIPPED:
        for path in (ROOT / package).rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    # A relative import is our own by definition.
                    if node.level or not node.module:
                        continue
                    names = [node.module]
                else:
                    continue
                for name in names:
                    top = name.split(".")[0]
                    found.setdefault(top, set()).add(
                        str(path.relative_to(ROOT)))
    return found


def declared() -> set[str]:
    out = set()
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        # fastapi==0.1, uvicorn[standard]==0.3, psycopg[binary]==3.3
        name = line.split("==")[0].split(">=")[0].split("[")[0].strip()
        out.add(name.lower().replace("_", "-"))
    return out


def test_every_third_party_import_is_declared():
    missing = {}
    have = declared()
    for module, files in sorted(imported_modules().items()):
        if module in OWN or module in sys.stdlib_module_names:
            continue
        dist = DIST.get(module, module).lower().replace("_", "-")
        if dist not in have:
            missing[f"{module} (as {dist})"] = sorted(files)
    assert not missing, (
        "imported but not in requirements.txt -- this is the deploy that "
        f"builds and then will not start:\n{missing}"
    )


def test_every_declared_package_is_actually_used():
    """A dependency nobody imports is an image that is bigger, slower to
    build and larger to audit than it needs to be. It is also usually a
    leftover from something that was removed."""
    used = set()
    for module in imported_modules():
        used.add(DIST.get(module, module).lower().replace("_", "-"))
    # Declared because something else needs them at runtime, not because
    # our own source imports them by name.
    indirect = {"uvicorn", "python-multipart"}
    unused = declared() - used - indirect
    assert not unused, f"in requirements.txt but imported nowhere: {sorted(unused)}"


@pytest.mark.parametrize("package", ["httpx", "pyjwt", "openpyxl"])
def test_the_runtime_dependencies_are_declared(package):
    """Named individually, so the diff on a change that drops one says
    which capability stops working rather than just 'a test failed'."""
    assert package in declared(), f"{package} is needed at runtime"


def test_pyjwt_is_declared_with_its_crypto_extra():
    """Without [crypto], PyJWT parses an ES256 or RS256 token and then
    cannot check its signature -- and a signature that is not checked is
    the only thing standing between a forged token and a tenant's data.

    This is its own test because the failure is quiet: the package is
    present, the import succeeds, and it breaks at the one moment that
    matters.
    """
    text = (ROOT / "requirements.txt").read_text()
    assert "pyjwt[crypto]" in text.lower(), (
        "declare pyjwt[crypto], not bare pyjwt")
