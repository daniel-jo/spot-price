"""Guard: every ``from .const import ...`` name must exist in const.py.

A dangling import makes the integration fail to import, which stops Home
Assistant from registering the config flow and surfaces as
``Invalid handler specified``. The Home Assistant runtime is not importable in
this repo's tests, so this static check catches that class of bug locally.
"""

from __future__ import annotations

import ast
import os

COMPONENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "spot_price"
)


def _const_names() -> set[str]:
    """All top-level names defined in const.py (assignments, functions, classes)."""
    tree = ast.parse(open(os.path.join(COMPONENT_DIR, "const.py")).read())
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def test_const_imports_resolve():
    defined = _const_names()
    missing: list[str] = []
    for filename in sorted(os.listdir(COMPONENT_DIR)):
        if not filename.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(COMPONENT_DIR, filename)).read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "const":
                for alias in node.names:
                    if alias.name not in defined:
                        missing.append(f"{filename}: from .const import {alias.name}")
    assert not missing, "Unresolved const imports:\n" + "\n".join(missing)


def _const_function_names() -> set[str]:
    """Module-level *functions* in const.py (the callable helpers)."""
    tree = ast.parse(open(os.path.join(COMPONENT_DIR, "const.py")).read())
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _available_names(tree: ast.AST) -> set[str]:
    """Every name a module can resolve without qualification."""
    available: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                available.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                available.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            available.add(node.name)
        elif isinstance(node, ast.arg):
            available.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            available.add(node.id)
    return available


def test_const_helpers_used_are_imported():
    """A bare reference to a const.py helper must be imported, not assumed in scope.

    ``coordinator.py`` once called ``normalize_area``/``_num``/``_clamp_forecast_days``
    without importing them, raising ``NameError: ... is not defined`` at integration
    setup. py_compile and a naive import check both miss that, so catch it statically.
    """
    helpers = _const_function_names()
    assert helpers, "const.py should define helper functions"
    problems: list[str] = []
    for filename in sorted(os.listdir(COMPONENT_DIR)):
        if not filename.endswith(".py") or filename == "const.py":
            continue
        tree = ast.parse(open(os.path.join(COMPONENT_DIR, filename)).read())
        available = _available_names(tree)
        used = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        for name in sorted(used & helpers - available):
            problems.append(f"{filename}: uses {name}() but never imports it")
    assert not problems, "Unresolved const helper references:\n" + "\n".join(problems)
