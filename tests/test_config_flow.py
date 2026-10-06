"""Guard: the setup and options forms must share one schema, so no field can
drift between the two Home Assistant entry flows.

Home Assistant is not importable here, so we statically inspect
``config_flow.py``: it must contain exactly one schema builder (``_shared_schema``)
and that builder must expose the full set of ``CONF_*`` keys in a single
``vol.Schema``.
"""

import ast

import pytest


CONFIG_FLOW_PATH = (
    "custom_components/spot_price/config_flow.py"
)

EXPECTED_KEYS = frozenset(
    {
        "CONF_AREA",
        "CONF_CURRENCY",
        "CONF_NAME",
        "CONF_API_KEY",
        "CONF_FX_MODE",
        "CONF_FIXED_FX",
        "CONF_VAT_PCT",
        "CONF_GRID_FEE",
        "CONF_WINDOW_HOURS",
        "CONF_UPDATE_INTERVAL",
        "CONF_FORECAST_DAYS",
    }
)


def _schema_keys(node: ast.Dict) -> frozenset[str]:
    """Extract every CONF_AREA-like key from a vol.Required/Optional dict."""
    keys: set[str] = set()
    for key_node in node.keys:
        if not isinstance(key_node, ast.Call):
            continue
        func = key_node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in {"Required", "Optional"}:
            continue
        if not isinstance(func.value, ast.Name) or func.value.id != "vol":
            continue
        if not key_node.args:
            continue
        name = key_node.args[0]
        if isinstance(name, ast.Name):
            keys.add(name.id)
    return frozenset(keys)


def _schema_dicts(tree: ast.AST) -> list[ast.Dict]:
    """Return all dict literals that feed a vol.Schema(...) call anywhere in the tree."""
    dicts: list[ast.Dict] = []

    def _walk(current: ast.AST) -> None:
        if isinstance(current, ast.Dict):
            dicts.append(current)
        for child in ast.iter_child_nodes(current):
            _walk(child)

    _walk(tree)
    return dicts


def test_shared_schema_exposes_every_config_key():
    """Both setup and options must build from the same _shared_schema dict."""
    tree = ast.parse(open(CONFIG_FLOW_PATH).read())
    matches = [d for d in _schema_dicts(tree) if _schema_keys(d) == EXPECTED_KEYS]
    assert matches, "No vol.Schema contains the full set of config keys"

    builder = ast.get_docstring(
        next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_shared_schema"
        )
    )
    assert builder is not None and "single source of truth" in builder

    # The shared dict must be the *only* schema literal in the file, otherwise the
    # options flow is still building its own separate schema.
    others = [
        d for d in _schema_dicts(tree) if d is not matches[0] and _schema_keys(d)
    ]
    assert not others, "Additional schema literal detected (setup/options drift?)"


def test_share_schema_defined_once():
    """The shared builder must exist exactly once."""
    tree = ast.parse(open(CONFIG_FLOW_PATH).read())
    count = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_shared_schema"
    )
    assert count == 1
if __name__ == "__main__":
    import sys
    import traceback

    failures = 0
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"PASS  {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failures} failed")
    sys.exit(1 if failures else 0)
