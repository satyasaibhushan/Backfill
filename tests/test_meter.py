from datetime import UTC, datetime

from backfill.config import Settings
from backfill.database import Database
from backfill.meter import Meter
from backfill.quota import QuotaError, QuotaService
from backfill.schemas import Observation, Window


async def test_both_meters_refresh_independently_and_failure_recovers(tmp_path, monkeypatch):
    service = QuotaService(Database(tmp_path / "quota.db"))
    meter = Meter(service, Settings(meter_enabled=False))
    meter.defaults()
    called = []
    fail = [True]

    async def read(provider, settings):
        called.append(provider)
        if provider == "claude" and fail[0]:
            raise QuotaError("Login unavailable", 503)
        now = datetime.now(UTC)
        return Observation(
            observed_at=now,
            covered_through=now,
            measurement="estimated",
            source="fixture",
            source_account=provider,
            windows=[
                Window(
                    name="weekly",
                    unit="quota_points",
                    limit=100,
                    used=12,
                    resets_at=datetime(2027, 1, 1, tzinfo=UTC),
                    duration_seconds=31536000,
                )
            ],
        )

    monkeypatch.setattr("backfill.meter.probe", read)
    await meter.refresh()
    assert set(called) == {"claude", "codex"}
    first = service.overview()["accounts"]
    assert next(a for a in first if a["key"] == "codex")["observation"]
    assert next(a for a in first if a["key"] == "claude")["observation"] is None
    fail[0] = False
    await meter.refresh()
    assert all(a["observation"] for a in service.overview()["accounts"])
    with service.database.transaction() as db:
        assert all(row["error"] is None for row in db.execute("SELECT error FROM meters"))
