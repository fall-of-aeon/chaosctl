import pytest

from chaosctl import alerts


@pytest.fixture(autouse=True)
def _isolated_alerts(tmp_path, monkeypatch):
    """Alerts go to a temp state dir and start empty in every test."""
    monkeypatch.setattr(alerts, "STATE_DIR", tmp_path)
    monkeypatch.setattr(alerts, "_seen", [])
    monkeypatch.setattr(alerts, "_once", set())
    monkeypatch.delenv("CHAOSCTL_REQUIRE_LLM", raising=False)
