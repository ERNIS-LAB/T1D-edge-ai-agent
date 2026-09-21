# tests/conftest.py
# Patches external services at the sys.modules level before any agent.* imports,
# so unit tests run without a real sensor, LM Studio, or apscheduler installed.
import os
import sys
from unittest.mock import MagicMock
import pytest

# ── Credentials ──────────────────────────────────────────────────────────────
# agent.tools skips building the LibreLinkUp client when no credentials are set,
# so the live-CGM tests need dummy ones in place before config is first imported.
# The client itself is mocked below, so these are never used against a real API.
# Set before importing config so a developer's real .env can't leak into tests
# (python-dotenv does not override variables that are already present).
os.environ.setdefault("LIBRE_EMAIL", "test@example.com")
os.environ.setdefault("LIBRE_PASSWORD", "test-password")

# ── PyLibreLinkUp ────────────────────────────────────────────────────────────
# tools.py runs authenticate() + get_patients() at module level, so we must
# intercept the class before the module is first imported.

_mock_patient = MagicMock()
_mock_patient.id = "test-patient"

_mock_client = MagicMock()
_mock_client.get_patients.return_value = [_mock_patient]
_mock_client.latest.return_value = MagicMock(
    __str__=lambda self: "GlucoseMeasurement(value=94, trend=Flat)"
)
_mock_client.logbook.return_value = []

sys.modules["pylibrelinkup"] = MagicMock(
    PyLibreLinkUp=MagicMock(return_value=_mock_client)
)

# ── pandas + glucostats ───────────────────────────────────────────────────────
# Mocked so unit tests don't require these libraries installed.
# Individual tests configure the glucostats mock return values as needed.
sys.modules["pandas"] = MagicMock()
sys.modules["glucostats"] = MagicMock()
sys.modules["glucostats.extract_statistics"] = MagicMock()

# ── APScheduler ──────────────────────────────────────────────────────────────
# May not be installed yet; mock it so scheduler tests don't depend on it.
_mock_apscheduler = MagicMock()
sys.modules["apscheduler"] = _mock_apscheduler
sys.modules["apscheduler.schedulers"] = _mock_apscheduler
sys.modules["apscheduler.schedulers.blocking"] = _mock_apscheduler
sys.modules["apscheduler.triggers"] = _mock_apscheduler
sys.modules["apscheduler.triggers.cron"] = _mock_apscheduler


@pytest.fixture(autouse=True)
def reset_sensor_client():
    """Restore the mock client + local stores to a clean default state between tests."""
    _mock_client.reset_mock()
    _mock_client.get_patients.return_value = [_mock_patient]
    _mock_client.latest.return_value = MagicMock(
        __str__=lambda self: "GlucoseMeasurement(value=94, trend=Flat)"
    )
    _mock_client.logbook.return_value = []

    # Lazy imports: avoid forcing agent module imports before mocks are installed.
    from agent.data.cgm_store import cgm_store
    from agent.kb.store import kb_store

    cgm_store.clear_all()
    kb_store.clear_all()
    yield
    cgm_store.clear_all()
    kb_store.clear_all()
