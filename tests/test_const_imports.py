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
