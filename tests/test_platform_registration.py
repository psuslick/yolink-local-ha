"""Regression tests for Home Assistant platform registration."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "yolocal"


def _platforms() -> set[str]:
    tree = ast.parse((COMPONENT / "const.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "PLATFORMS":
            return {elt.value for elt in node.value.elts}
    raise AssertionError("PLATFORMS not found")


def test_implemented_entity_platforms_are_forwarded() -> None:
    non_entity_modules = {
        "__init__", "api", "availability", "config_flow", "const", "coordinator",
        "device_events", "device_trigger", "diagnostics", "dimmer", "entity",
        "event_capture", "models", "outlet_power",
    }
    implemented = {
        path.stem
        for path in COMPONENT.glob("*.py")
        if path.stem not in non_entity_modules
    }
    registered = _platforms()
    assert implemented <= registered
    assert "light" in registered
