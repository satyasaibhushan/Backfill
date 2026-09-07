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


def test_dashboard_keeps_fresh_reading_when_admission_rejects_it(tmp_path, monkeypatch):
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from backfill.auth import read_secret
    from backfill.main import create_app

    settings = Settings(data_dir=tmp_path, automation_enabled=False, meter_enabled=False)
    used = [12]
    fail = [False]
    reset = datetime.now(UTC) + timedelta(hours=4)

    async def read(provider, settings):
        if fail[0]:
            raise QuotaError("Reader unavailable", 503)
        now = datetime.now(UTC)
        return Observation(
            observed_at=now,
            covered_through=now,
            measurement="estimated",
            source="fixture",
            source_account=provider,
            windows=[
                Window(
                    name="session",
                    unit="quota_points",
                    limit=100,
                    used=used[0],
                    resets_at=reset,
                    duration_seconds=18000,
                )
            ],
        )

    monkeypatch.setattr("backfill.meter.probe", read)
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.meter.defaults()
        headers = {"Authorization": "Bearer " + read_secret(settings.root / "owner.token")}
        client.portal.call(app.state.meter.refresh)
        used[0] = 10
        client.portal.call(app.state.meter.refresh)
        result = client.get("/v2/overview", headers=headers).json()
        for account in result["accounts"]:
            assert account["connected"] is True
            assert account["execution_ready"] is False
            assert account["windows"][0]["remaining"] == 90
        assert all(
            a["observation"]["windows"][0]["used"] == 12
            for a in app.state.quota.overview()["accounts"]
        )
        fail[0] = True
        client.portal.call(app.state.meter.refresh)
        assert all(
            not a["connected"]
            for a in client.get("/v2/overview", headers=headers).json()["accounts"]
        )
        fail[0], used[0] = False, 14
        client.portal.call(app.state.meter.refresh)
        assert all(
            a["connected"] and a["execution_ready"]
            for a in client.get("/v2/overview", headers=headers).json()["accounts"]
        )


def test_model_quota_is_separate_and_missing_reading_stays_held(tmp_path, monkeypatch):
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from backfill.auth import read_secret
    from backfill.main import create_app

    settings = Settings(data_dir=tmp_path, automation_enabled=False, meter_enabled=False)
    include_model = [True]
    reset = datetime.now(UTC) + timedelta(days=5)

    async def read(provider, settings):
        now = datetime.now(UTC)
        windows = [
            Window(
                name="secondary",
                unit="quota_points",
                limit=100,
                used=3,
                resets_at=reset,
                duration_seconds=604800,
            )
        ]
        if provider == "claude" and include_model[0]:
            windows.append(
                Window(
                    name="extra.claude-weekly-scoped-fable",
                    unit="quota_points",
                    limit=100,
                    used=80,
                    resets_at=reset,
                    duration_seconds=604800,
                )
            )
        return Observation(
            observed_at=now,
            covered_through=now,
            measurement="estimated",
            source="fixture",
            source_account=provider,
            windows=windows,
        )

    monkeypatch.setattr("backfill.meter.probe", read)
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.meter.defaults()
        headers = {"Authorization": "Bearer " + read_secret(settings.root / "owner.token")}

        def accounts():
            return {
                a["provider"]: a
                for a in client.get("/v2/overview", headers=headers).json()["accounts"]
            }

        client.portal.call(app.state.meter.refresh)
        first = accounts()
        assert [(w["label"], w["remaining"]) for w in first["claude"]["windows"]] == [
            ("Weekly", 97),
            ("Fable weekly", 20),
        ]
        assert [w["label"] for w in first["codex"]["windows"]] == ["Weekly"]
        include_model[0] = False
        client.portal.call(app.state.meter.refresh)
        missing = accounts()["claude"]
        assert missing["connected"] is True
        assert missing["execution_ready"] is False
        assert missing["windows"][-1] == {
            "label": "Fable weekly",
            "remaining": None,
            "resets_at": None,
        }
        include_model[0] = True
        client.portal.call(app.state.meter.refresh)
        assert accounts()["claude"]["execution_ready"] is True
