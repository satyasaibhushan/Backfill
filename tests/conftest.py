from datetime import UTC, datetime

import pytest

from backfill.database import Database
from backfill.quota import QuotaService
from backfill.schemas import Observation, Policy, Window, WorkloadInput


@pytest.fixture(autouse=True)
def offline_defaults(monkeypatch):
    monkeypatch.setenv("BACKFILL_AUTOMATION_ENABLED", "false")
    monkeypatch.setenv("BACKFILL_METER_ENABLED", "false")
    monkeypatch.setenv("BACKFILL_DASHBOARD_PORT", "0")


@pytest.fixture
def setup(tmp_path):
    clock = [datetime(2026, 9, 7, 9, tzinfo=UTC).timestamp()]
    service = QuotaService(Database(tmp_path / "quota.db"), lambda: clock[0])
    service.set_account("account", Policy())
    service.set_workload("bulk", WorkloadInput(account="account", priority=0))
    service.set_workload("urgent", WorkloadInput(account="account", priority=100))
    return service, clock


@pytest.fixture
def observe(setup):
    service, clock = setup
    first = clock[0]

    def write(used=0, weekly=0, reset=None, coverage=None, **changes):
        payload = dict(
            observed_at=datetime.fromtimestamp(clock[0], UTC),
            covered_through=datetime.fromtimestamp(
                clock[0] - 1 if coverage is None else coverage, UTC
            ),
            source="test",
            source_account="fixed-account",
            measurement="measured",
            windows=[
                Window(
                    name=name,
                    unit="points",
                    limit=100,
                    used=value,
                    resets_at=datetime.fromtimestamp(end, UTC),
                    duration_seconds=duration,
                )
                for name, value, end, duration in (
                    ("session", used, reset or first + 3600, 18000),
                    ("weekly", weekly, first + 604800, 604800),
                )
            ],
        )
        payload.update(changes)
        return service.observe("account", Observation(**payload))

    write()
    return write
