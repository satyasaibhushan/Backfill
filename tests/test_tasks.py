from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backfill.auth import read_secret
from backfill.config import Settings
from backfill.database import Database
from backfill.main import create_app
from backfill.meter import Meter
from backfill.quota import QuotaError, QuotaService
from backfill.schemas import Observation, Window
from backfill.tasks import Preferences, ProjectInput, TaskAction, TaskInput, Tasks


@pytest.fixture
def tasks(tmp_path):
    clock = [datetime(2026, 9, 7, tzinfo=UTC).timestamp()]
    quota = QuotaService(Database(tmp_path / "quota.db"), lambda: clock[0])
    settings = Settings(data_dir=tmp_path, meter_enabled=False, automation_enabled=False)
    meter = Meter(quota, settings)
    meter.defaults()
    app = Tasks(quota, settings)

    def observe(provider, used=0, offset=0):
        now = datetime.fromtimestamp(clock[0], UTC)
        quota.observe(
            provider,
            Observation(
                observed_at=now,
                covered_through=now,
                measurement="measured",
                source="fixture",
                source_account=provider,
                windows=[
                    Window(
                        name="weekly",
                        unit="quota_points",
                        limit=100,
                        used=used,
                        resets_at=datetime(2026, 9, 14, tzinfo=UTC) + timedelta(days=offset),
                        duration_seconds=604800,
                    )
                ],
            ),
        )

    observe("claude", 2)
    observe("codex", 98)
    return app, clock, observe


def new(app, **values):
    return app.create(
        TaskInput(title="Read the project", instructions="Summarize the public API", **values)
    )


def test_real_instructions_projects_and_restart_persistence(tasks):
    app, clock, _ = tasks
    project = app.project(ProjectInput(name="Research"))
    job = new(app, project=project["id"])
    assert job["instructions"] == "Summarize the public API"
    assert job["state"] == "queued" and not job["paused_until"]
    assert app.select(job)[0] == "claude"
    again = Tasks(app.quota, app.settings)
    assert again.get(job["id"])["title"] == job["title"]
    assert again.list()["projects"][0]["name"] == "Research"


def test_priority_timed_pause_global_reserve_and_no_bypass(tasks):
    app, clock, _ = tasks
    low = new(app, priority="low")
    high = new(app, priority="high")
    assert app.candidates()[0]["id"] == high["id"]
    app.action(
        high["id"], TaskAction(action="pause", until=datetime.fromtimestamp(clock[0] + 60, UTC))
    )
    assert app.candidates()[0]["id"] == low["id"]
    clock[0] += 61
    assert app.candidates()[0]["id"] == high["id"]
    app.set_preferences(Preferences(paused=True))
    assert app.select(low)[0] is None
    app.set_preferences(Preferences(reserve=80))
    assert app.select(new(app, provider="codex", priority="high"))[0] is None


def test_allowance_charges_account_movement_and_shared_project(tasks):
    app, clock, observe = tasks
    p = app.project(ProjectInput(name="Shared", allowance=5))
    first = new(app, project=p["id"], allowance=3)
    second = new(app, project=p["id"])
    attempt = app.start(first["id"], "claude")
    clock[0] += 10
    observe("claude", 8)
    app.charge(attempt)
    app.finish(first["id"], attempt["id"], "waiting")
    assert app.select(app.get(first["id"]))[0] is None
    assert app.select(second)[0] is None
    windows = app.quota.overview()["accounts"][0]["observation"]["windows"]
    assert app.spending(first, "claude", windows) == (6, 6)


def test_review_gate_feedback_and_completion(tasks):
    app, clock, _ = tasks
    job = new(app)
    with pytest.raises(QuotaError):
        app.action(job["id"], TaskAction(action="approve"))
    attempt = app.start(job["id"], "claude")
    app.progress(job["id"], attempt["id"], "The real result")
    app.finish(job["id"], attempt["id"], "review")
    assert not app.candidates()
    changed = app.action(job["id"], TaskAction(action="revise", feedback="Add evidence"))
    assert changed["feedback"] == "Add evidence" and changed["state"] == "queued"
    attempt = app.start(job["id"], "claude")
    app.finish(job["id"], attempt["id"], "review")
    accepted = app.action(job["id"], TaskAction(action="approve"))
    assert accepted["state"] == "done"
    assert not app.candidates()


def test_restart_marks_running_work_for_attention_and_preserves_output(tasks):
    app, _, _ = tasks
    job = new(app)
    attempt = app.start(job["id"], "claude")
    app.progress(job["id"], attempt["id"], "Partial evidence")
    app.recover()
    result = app.get(job["id"])
    assert result["state"] == "failed" and result["output"] == "Partial evidence"
    assert result["attempts"][0]["state"] == "interrupted"
    assert app.action(job["id"], TaskAction(action="retry"))["state"] == "queued"


def test_api_rejects_unauthorized_actions_and_hides_internal_windows(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "private", automation_enabled=False, meter_enabled=False
    )
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": "Bearer " + read_secret(settings.root / "owner.token")}
        assert client.post("/v2/tasks", json={"title": "x", "instructions": "y"}).status_code == 401
        result = client.post(
            "/v2/tasks", headers=headers, json={"title": "Inspect", "instructions": "Read files"}
        )
        assert result.status_code == 201
        key = result.json()["id"]
        overview = client.get("/v2/overview", headers=headers).json()
        assert overview["tasks"][0]["id"] == key
        assert "instructions" not in overview["tasks"][0]
        assert (
            client.get("/v2/tasks/" + key, headers=headers).json()["instructions"] == "Read files"
        )
        assert (
            client.post(
                "/v2/tasks/" + key + "/actions",
                headers={**headers, "Origin": "https://elsewhere.invalid"},
                json={"action": "pause"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v2/tasks",
                headers=headers,
                json={"title": "x", "instructions": "y", "folder": "/missing/folder"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v2/tasks",
                headers=headers,
                json={"title": "x", "instructions": "y", "source_url": "javascript:alert(1)"},
            ).status_code
            == 422
        )


def test_review_is_immutable_and_project_accounting_cannot_be_moved(tasks):
    app, _, _ = tasks
    project = app.project(ProjectInput(name="Other"))
    job = new(app)
    attempt = app.start(job["id"], "claude")
    app.finish(job["id"], attempt["id"], "review")
    with pytest.raises(QuotaError):
        app.update(job["id"], TaskInput(title="Changed", instructions="Different work"))
    with pytest.raises(QuotaError):
        app.action(job["id"], TaskAction(action="pause"))
    app.action(job["id"], TaskAction(action="revise", feedback="More detail"))
    with pytest.raises(QuotaError):
        app.update(
            job["id"],
            TaskInput(title="Changed", instructions="Different work", project=project["id"]),
        )


def test_allowance_resets_with_provider_window(tasks):
    app, clock, observe = tasks
    job = new(app, allowance=3)
    attempt = app.start(job["id"], "claude")
    clock[0] += 10
    clock[0] += 604800
    observe("claude", 6, offset=7)
    app.charge(attempt)
    windows = app.quota.overview()["accounts"][0]["observation"]["windows"]
    assert app.spending(job, "claude", windows) == (6, 0)
    app.finish(job["id"], attempt["id"], "waiting")
    assert app.select(app.get(job["id"]))[0] is None


def test_personal_reserve_is_a_floor_after_history_is_available(tasks):
    app, _, _ = tasks
    app.set_preferences(Preferences(reserve=60))
    for account in app.quota.overview()["accounts"]:
        assert account["policy"]["minimum_user_percent"] == 60
        assert account["policy"]["cold_start_user_percent"] == 60


def test_scheduling_is_disabled_and_links_become_instructions(tasks):
    app, clock, _ = tasks
    with pytest.raises(QuotaError, match="Scheduling"):
        new(app, schedule="daily")
    with pytest.raises(QuotaError, match="Scheduling"):
        new(app, scheduled_at=datetime.fromtimestamp(clock[0] + 60, UTC))
    job = new(app, source_url="https://example.com/task")
    assert not job["source_url"] and job["instructions"].endswith("https://example.com/task")


def test_consumption_survives_completion_and_sums_revisions(tasks):
    app, clock, observe = tasks
    job = new(app)
    assert app.get(job["id"])["consumption"]["windows"] == []
    for used in (4, 7):
        attempt = app.start(job["id"], "claude")
        clock[0] += 10
        observe("claude", used)
        app.charge(attempt)
        app.finish(job["id"], attempt["id"], "review")
        if used == 4:
            app.action(job["id"], TaskAction(action="revise", feedback="More detail"))
    app.action(job["id"], TaskAction(action="approve"))
    restored = Tasks(app.quota, app.settings).get(job["id"])
    assert restored["consumption"] == {
        "windows": [{"provider": "claude", "label": "Weekly", "used": 5}],
        "incomplete": False,
    }
    clock[0] += 604800
    observe("claude", 1, offset=7)
    assert app.get(job["id"])["consumption"] == restored["consumption"]


def test_reset_during_run_preserves_total_and_timestamp_jitter_is_not_new_usage(tasks):
    app, clock, observe = tasks
    job = new(app)
    attempt = app.start(job["id"], "claude")
    clock[0] += 10
    observe("claude", 4)
    app.charge(attempt)
    clock[0] += 120
    observe("claude", 4, offset=1 / 1440)
    app.charge(attempt)
    assert app.get(job["id"])["consumption"]["windows"][0]["used"] == 2
    clock[0] += 604800
    observe("claude", 3, offset=7)
    app.charge(attempt)
    assert app.get(job["id"])["consumption"]["windows"][0]["used"] == 5


def test_legacy_consumption_corrects_reset_timestamp_jitter(tasks):
    import json

    app, clock, _ = tasks
    job = new(app)
    attempt = app.start(job["id"], "claude")
    original = attempt["baseline"]["windows"][0]
    shifted = (datetime.fromisoformat(original["resets_at"]) + timedelta(seconds=30)).isoformat()
    with app.quota.database.transaction() as db:
        db.execute(
            "UPDATE attempts SET spending=? WHERE id=?",
            (json.dumps({"weekly": {"used": 4, "reset": shifted}}), attempt["id"]),
        )
    clock[0] += 60
    app.finish(job["id"], attempt["id"], "review")
    assert app.get(job["id"])["consumption"]["windows"][0]["used"] == 2


def test_auto_retry_reselects_provider_but_explicit_selection_stays(tasks):
    app, clock, observe = tasks
    job = new(app)
    job["selected_provider"] = "codex"
    assert app.select(job)[0] == "claude"
    job["provider"] = "codex"
    assert app.select(job)[0] is None
    clock[0] += 7 * 86400 + 1
    observe("codex", 1, offset=7)
    assert app.select(job)[0] == "codex"


def test_capacity_wait_does_not_become_failure_after_three_attempts(tasks):
    app, clock, _ = tasks
    job = new(app)
    for _ in range(5):
        attempt = app.start(job["id"], "claude")
        assert attempt
        app.finish(job["id"], attempt["id"], "waiting", "Allowance reached")
        assert app.get(job["id"])["state"] == "waiting"
        clock[0] += 61
    assert app.get(job["id"])["attempt"] == 5
