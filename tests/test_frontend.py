"""Checks for the bundled Lovelace dashboard card.

These tests run in a plain-stdlib environment (HA core is not installed), so
they assert on the project layout and the wiring in `__init__.py` instead of
importing the component.
"""

from __future__ import annotations

import json
import os

COMPONENT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "spot_price"
)
FRONTEND_FILE = os.path.join(COMPONENT, "frontend", "spot-price-card.js")


def test_frontend_card_exists_and_registers_custom_element():
    assert os.path.isfile(FRONTEND_FILE), "frontend/spot-price-card.js is missing"
    with open(FRONTEND_FILE, encoding="utf-8") as handle:
        src = handle.read()
    assert len(src) > 3000
    assert '"use strict"' in src
    assert 'customElements.define("spot-price-card"' in src
    assert "spot-price-card" in src
    assert "customCards" in src


def test_init_serves_card_and_manifest_stays_pure():
    with open(os.path.join(COMPONENT, "__init__.py"), encoding="utf-8") as handle:
        init_src = handle.read()
    assert "register_static_path" in init_src
    assert "async_register_static_paths" in init_src
    assert "spot-price-card.js" in init_src
    assert 'FRONTEND_URL_BASE = "/spot_price"' in init_src

    with open(os.path.join(COMPONENT, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert manifest.get("requirements") == []


def test_frontend_references_match_disk():
    with open(os.path.join(COMPONENT, "__init__.py"), encoding="utf-8") as handle:
        init_src = handle.read()
    # The static route points at the directory holding the card file.
    frontend_dir_line = next(
        line for line in init_src.splitlines() if "FRONTEND_DIRECTORY =" in line
    )
    assert "frontend" in frontend_dir_line
    assert os.path.isdir(os.path.join(COMPONENT, "frontend"))