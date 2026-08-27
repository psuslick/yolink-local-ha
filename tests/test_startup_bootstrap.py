"""Regression checks for transient startup recovery."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "custom_components/yolocal/coordinator.py").read_text(encoding="utf-8")


def test_transient_bootstrap_failure_queues_immediate_targeted_retry() -> None:
    assert 'source="bootstrap"' in TEXT
    assert 'reason="bootstrap_transient_retry"' in TEXT
    assert "force=True" in TEXT
    assert "self._device_io_lock = asyncio.Lock()" in TEXT
    assert "await self._async_get_state_runtime(device)" in TEXT
