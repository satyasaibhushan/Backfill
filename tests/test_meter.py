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


def test_model_quota_partial_sample_preserves_fresh_complete_reading(tmp_path, monkeypatch):
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
        assert missing["execution_ready"] is True
        assert missing["windows"] == first["claude"]["windows"]
        # A cached complete sample must expire even if partial samples keep arriving.
        original_clock = app.state.quota.clock
        monkeypatch.setattr(app.state.quota, "clock", lambda: original_clock() + 181)
        client.portal.call(app.state.meter.refresh)
        assert accounts()["claude"]["execution_ready"] is False
        monkeypatch.setattr(app.state.quota, "clock", original_clock)
        include_model[0] = True
        client.portal.call(app.state.meter.refresh)
        assert accounts()["claude"]["execution_ready"] is True


def test_reset_confirmation_state_clears_after_coverage_passes_reset(tmp_path, monkeypatch):
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from backfill.auth import read_secret
    from backfill.main import create_app

    now = datetime.now(UTC)
    reset = now - timedelta(seconds=10)
    stage = [0]
    times = [now - timedelta(seconds=200), now, now + timedelta(seconds=121)]
    settings = Settings(data_dir=tmp_path, automation_enabled=False, meter_enabled=False)

    async def read(provider, settings):
        observed = times[stage[0]]
        windows = [
            Window(
                name="primary",
                unit="quota_points",
                limit=100,
                used=3 if stage[0] == 0 else 0,
                resets_at=reset if stage[0] == 0 else reset + timedelta(hours=5),
                duration_seconds=18000,
            )
        ]
        if provider == "claude":
            windows.append(
                Window(
                    name="extra.claude-weekly-scoped-fable",
                    unit="quota_points",
                    limit=100,
                    used=0,
                    resets_at=now + timedelta(days=5),
                    duration_seconds=604800,
                )
            )
        return Observation(
            observed_at=observed,
            covered_through=observed - timedelta(seconds=120),
            source="fixture",
            source_account=provider,
            measurement="estimated",
            windows=windows,
        )

    monkeypatch.setattr("backfill.meter.probe", read)
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.meter.defaults()
        app.state.quota.clock = lambda: times[stage[0]].timestamp()
        headers = {"Authorization": "Bearer " + read_secret(settings.root / "owner.token")}
        client.portal.call(app.state.meter.refresh)
        stage[0] = 1
        client.portal.call(app.state.meter.refresh)
        accounts = client.get("/v2/overview", headers=headers).json()["accounts"]
        assert len(accounts) == 2
        for account in accounts:
            assert account["connected"] is True
            assert account["execution_ready"] is False
            assert account["windows"][0]["remaining"] == 100
            assert account["status"] == {
                "code": "reset_pending",
                "label": "Waiting for reset confirmation",
            }
        stage[0] = 2
        client.portal.call(app.state.meter.refresh)
        for account in client.get("/v2/overview", headers=headers).json()["accounts"]:
            assert account["execution_ready"] is True
            assert account["status"] == {"code": "ready", "label": ""}


async def test_native_correction_requires_three_spaced_readings_and_survives_restart(
    tmp_path, monkeypatch
):
    from datetime import timedelta

    clock = [datetime(2026, 9, 8, tzinfo=UTC).timestamp()]
    service = QuotaService(Database(tmp_path / "quota.db"), lambda: clock[0])
    meter = Meter(service, Settings())
    meter.defaults()
    reset = datetime.fromtimestamp(clock[0], UTC) + timedelta(days=6)
    used = [59]

    async def read(provider, settings):
        now = datetime.fromtimestamp(clock[0], UTC)
        return Observation(
            observed_at=now,
            covered_through=now,
            measurement="estimated",
            source="codex-app-server",
            source_account=provider,
            windows=[
                Window(
                    name="weekly",
                    unit="quota_points",
                    limit=100,
                    used=used[0],
                    resets_at=reset,
                    duration_seconds=604800,
                )
            ],
        )

    monkeypatch.setattr("backfill.meter.probe", read)
    await meter._one("codex", "codex")
    used[0] = 3
    for seconds in (45, 60):
        clock[0] += seconds
        await meter._one("codex", "codex")
        assert (
            next(a for a in service.overview()["accounts"] if a["key"] == "codex")["observation"][
                "windows"
            ][0]["used"]
            == 59
        )
    meter = Meter(service, Settings())
    clock[0] += 60
    await meter._one("codex", "codex")
    with service.database.transaction() as db:
        row = db.execute("SELECT observation FROM accounts WHERE key='codex'").fetchone()
        assert Observation.model_validate_json(row[0]).windows[0].used == 3
        assert db.execute("SELECT error FROM meters WHERE account='codex'").fetchone()[0] is None
