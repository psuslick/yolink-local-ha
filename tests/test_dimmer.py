"""Regression tests for YS5707 brightness conversion helpers."""

import importlib.util
from pathlib import Path
import sys

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "yolocal"
    / "dimmer.py"
)
spec = importlib.util.spec_from_file_location("yolocal_dimmer_test", MODULE_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def test_yolink_100_percent_maps_to_ha_255() -> None:
    assert mod.yolink_brightness_to_ha(100) == 255


def test_ha_255_maps_to_yolink_100_percent() -> None:
    assert mod.ha_brightness_to_yolink(255) == 100


def test_midpoint_round_trip_is_close() -> None:
    pct = mod.ha_brightness_to_yolink(128)
    assert pct == 50
    assert abs(mod.yolink_brightness_to_ha(pct) - 128) <= 1


def test_values_are_clamped() -> None:
    assert mod.yolink_brightness_to_ha(150) == 255
    assert mod.ha_brightness_to_yolink(-20) == 0
