"""Structural guard: no order-placement capability may exist in this repo.

``BRIEFING.md`` §5 says no live capital and no real order placement, and the user
restated it as "No Live trading at all". Both are intentions, and an intention is
not a property of the code -- so these tests convert it into one.

Shadow mode is the closest this project gets: it consumes the live feed on the
live clock, simulates fills against the observed book, and places nothing. The
difference between that and live trading is exactly the set of names below.

**Prose is exempt; code is not.** The checks run over the AST, so a docstring that
explains *why* ``set_ip`` is avoided does not read as a violation while
``dhan.set_ip(...)`` does. A text grep cannot tell those apart, and the version of
this file that used one forced the modules to stay silent about their own
restraint -- which made the repo less clear, not safer.

None of it needs ``dhanhq`` installed: everything works on source text, so the
guard still runs in a bare checkout and cannot be defeated by an import failure.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from .conftest import REPO_ROOT

SRC = REPO_ROOT / "src"

# dhanhq submodules the project is permitted to reach, directly or transitively.
# `_order`, `_super_order` and `_forever_order` are absent by construction, which
# is the point: order placement is not reachable, not merely unused.
ALLOWED_DHANHQ = frozenset(
    {
        "dhanhq",
        "dhanhq.auth",
        "dhanhq.constants",
        "dhanhq.dhan_context",
        "dhanhq.dhan_http",
        "dhanhq.dhanhq",
        "dhanhq.fulldepth",
        "dhanhq.marketfeed",
        "dhanhq._historical_data",
        "dhanhq._market_feed",
        "dhanhq._option_chain",
    }
)

# Method names that exist only to place, amend or withdraw a live order -- plus
# the two IP-registration calls, which are an order-placement prerequisite and
# nothing else. Checked against identifiers and attribute accesses.
FORBIDDEN_NAMES = (
    "place_order",
    "place_slice_order",
    "modify_order",
    "cancel_order",
    "cancel_all_orders",
    "place_super_order",
    "place_forever_order",
    "set_ip",
    "modify_ip",
)

# Route fragments, checked against non-docstring string literals so a hand-rolled
# HTTP call cannot bypass the name check above.
FORBIDDEN_ROUTES = ("/orders", "/super/orders", "/forever/orders", "/ip/setIP", "/ip/modifyIP")

# Packages that must not exist. A directory named `execution/` invites exactly the
# code these tests forbid, so its absence is asserted rather than assumed.
FORBIDDEN_PACKAGES = ("broker", "execution", "oms", "trading")


def _source_files() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    assert files, f"no python files found under {SRC}; the guard would pass vacuously"
    return files


def _parse(text: str, filename: str = "<test>") -> ast.Module:
    return ast.parse(text, filename=filename)


def imported_modules(tree: ast.Module) -> set[str]:
    """Module paths imported, including aliased and ``from`` imports.

    AST rather than regex so ``import dhanhq._order as x`` and
    ``from dhanhq import _order`` are both caught.

    ``from X import Y`` is ambiguous in the AST: ``Y`` may be a submodule of ``X``
    or an ordinary symbol defined in it. It is resolved by composing ``X.Y`` only
    when ``X`` is a package root -- ``dhanhq`` is flat, so ``dhanhq.auth`` is a
    leaf module whose names are classes, while ``dhanhq`` itself is the package
    whose names may be submodules. If that layout ever changes, the new module
    path shows up in the allowlist check anyway.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            if "." not in node.module:  # package root, so names may be submodules
                found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def referenced_names(tree: ast.Module) -> set[str]:
    """Identifiers the code actually uses, excluding anything inside prose."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.Import):
            names.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """``id()`` of every docstring constant, so prose can be excluded."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def code_strings(tree: ast.Module) -> list[str]:
    """String literals that are not docstrings.

    ``getattr(dhan, "place_order")`` hides the name from :func:`referenced_names`,
    so literals are checked too -- but a module docstring discussing the name is
    documentation, and excluding docstrings is what keeps the two distinguishable.
    """
    skip = _docstring_nodes(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
    ]


# --- the guard ----------------------------------------------------------------


def test_only_market_data_submodules_of_dhanhq_are_imported() -> None:
    """An allowlist, so a new order-placement import fails rather than passes."""
    offenders: dict[str, set[str]] = {}
    for path in _source_files():
        tree = _parse(path.read_text(encoding="utf-8"), str(path))
        used = {m for m in imported_modules(tree) if m.split(".")[0] == "dhanhq"}
        bad = used - ALLOWED_DHANHQ
        if bad:
            offenders[str(path.relative_to(REPO_ROOT))] = bad
    assert not offenders, (
        "order-placement-capable dhanhq imports found. This project is read-only "
        f"against the broker API: {offenders}"
    )


@pytest.mark.parametrize("name", FORBIDDEN_NAMES)
def test_no_order_placement_names_are_referenced(name: str) -> None:
    """A backstop for what the import allowlist cannot see.

    An allowlisted module can still expose an order method -- ``dhanhq.dhanhq``
    aggregates everything -- so the call sites are forbidden by name too.
    """
    hits = []
    for path in _source_files():
        tree = _parse(path.read_text(encoding="utf-8"), str(path))
        rel = path.relative_to(REPO_ROOT)
        if name in referenced_names(tree):
            hits.append(f"{rel} (identifier)")
        if any(name in s for s in code_strings(tree)):
            hits.append(f"{rel} (string literal)")
    assert not hits, f"{name!r} must not be referenced in src/: {hits}"


@pytest.mark.parametrize("route", FORBIDDEN_ROUTES)
def test_no_order_placement_routes_appear_in_src(route: str) -> None:
    hits = [
        str(path.relative_to(REPO_ROOT))
        for path in _source_files()
        if any(route in s for s in code_strings(_parse(path.read_text(encoding="utf-8"), str(path))))
    ]
    assert not hits, f"the {route!r} route must not be constructed in src/: {hits}"


@pytest.mark.parametrize("name", FORBIDDEN_PACKAGES)
def test_no_execution_package_exists(name: str) -> None:
    found = [str(p.relative_to(REPO_ROOT)) for p in SRC.rglob(name) if p.is_dir()]
    assert not found, (
        f"a {name!r} package must not exist: there is no order-placement layer in "
        f"this project, so there is nothing for it to hold. Found {found}"
    )


# --- guard on the guard -------------------------------------------------------
# A test whose failure mode is never exercised is decoration. If the detectors
# silently stop matching -- a rename, a refactor, an AST node type that moved --
# every test above starts passing for the wrong reason. So the detectors are run
# against source that must trip them, and against prose that must not.

OFFENDING_SOURCE = '''
"""A module that talks about place_order in its docstring only."""
from dhanhq._order import Order
from dhanhq import _forever_order
import dhanhq._super_order as so


def go(dhan, http):
    dhan.place_order(security_id=1)
    getattr(dhan, "cancel_order")(2)
    http.post("/orders", {})
'''

INNOCENT_SOURCE = '''
"""Deliberately does not call place_order or set_ip. See /orders in the docs."""
from dhanhq.marketfeed import MarketFeed


def go(feed):
    """Nothing here places an order: no place_order, no /orders."""
    return feed.subscribe()
'''


def test_detectors_fire_on_offending_source() -> None:
    tree = _parse(OFFENDING_SOURCE)
    modules = imported_modules(tree)
    assert {"dhanhq._order", "dhanhq._super_order"} <= modules, "direct paths"
    assert "dhanhq._forever_order" in modules, "`from dhanhq import _x` must compose"
    assert not modules <= ALLOWED_DHANHQ
    assert "place_order" in referenced_names(tree), "attribute call must be seen"
    assert any("cancel_order" in s for s in code_strings(tree)), "getattr must be seen"
    assert any("/orders" in s for s in code_strings(tree)), "raw route must be seen"


def test_detectors_ignore_prose() -> None:
    """The exemption that makes the guard usable, pinned so it stays narrow.

    Docstrings may name these things; code may not. If this ever inverts, the
    modules go back to being unable to document their own constraints.
    """
    tree = _parse(INNOCENT_SOURCE)
    assert imported_modules(tree) <= ALLOWED_DHANHQ
    assert "place_order" not in referenced_names(tree)
    assert "set_ip" not in referenced_names(tree)
    strings = code_strings(tree)
    assert not any("place_order" in s for s in strings)
    assert not any("/orders" in s for s in strings), "docstring routes are prose"


def test_a_class_import_is_not_mistaken_for_a_submodule() -> None:
    """Regression: ``from dhanhq.auth import DhanLogin`` is not ``dhanhq.auth.DhanLogin``.

    The first version of :func:`imported_modules` composed every ``from`` import,
    which made a legitimate class import look like an unlisted submodule -- a
    guard that fires on correct code gets relaxed, and then it guards nothing.
    """
    tree = _parse("from dhanhq.auth import DhanLogin\n")
    assert imported_modules(tree) == {"dhanhq.auth"}
    assert imported_modules(tree) <= ALLOWED_DHANHQ


def test_the_forbidden_sets_are_not_empty() -> None:
    assert "place_order" in FORBIDDEN_NAMES
    assert "dhanhq._order" not in ALLOWED_DHANHQ
    assert FORBIDDEN_ROUTES and FORBIDDEN_PACKAGES
