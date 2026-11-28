"""General functional tests omit elapsed wall-clock waits; timing has dedicated cases."""
import pytest

@pytest.fixture(autouse=True)
def functional_tests_without_research_delay(monkeypatch):
    from apps.api.modules import core
    monkeypatch.setattr(core,'INSIGHT_MIN_SECONDS',0)

    from apps.api.modules import synthesis
    monkeypatch.setattr(synthesis,'MIN_RESEARCH_SECONDS',0)
