"""Guard: the integration manifest stays dependency-free and version-synced.

Home Assistant is not importable in this repo's tests, so these checks inspect
the files statically. `manifest.json` must not grow runtime requirements (the
integration is pure stdlib), and its `version` must match `const.VERSION` —
drift between the two surfaces as a wrong version in HACS/Home Assistant.
"""

from __future__ import annotations

import ast
import json
import os

COMPONENT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "spot_price"
)


def _manifest() -> dict:
    with open(os.path.join(COMPONENT, "manifest.json"), encoding="utf-8") as handle:
        return json.load(handle)


def _const_version() -> str:
    with open(os.path.join(COMPONENT, "const.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "VERSION"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("const.py defines no VERSION constant")


def test_manifest_has_no_runtime_requirements():
    assert _manifest().get("requirements") == []


def test_manifest_version_matches_const():
    assert _manifest().get("version") == _const_version()


if __name__ == "__main__":
    test_manifest_has_no_runtime_requirements()
    test_manifest_version_matches_const()
    print("PASS  test_manifest")
