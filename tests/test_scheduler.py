from backfill.models import Task
from backfill.providers.models import ProviderSnapshot, UsageWindow
from backfill.scheduler import Scheduler


def snapshot(provider: str, session_remaining: float, weekly_remaining: float):
    return ProviderSnapshot(
        provider_id=provider,
        ready=True,
        source="test",
        windows=[
            UsageWindow(
                name="session",
                used_percent=100 - session_remaining,
                remaining_percent=session_remaining,
            ),
            UsageWindow(
                name="weekly",
                used_percent=100 - weekly_remaining,
                remaining_percent=weekly_remaining,
            ),
        ],
    )


def task(**overrides) -> Task:
    values = {
        "title": "Bounded maintenance",
        "instructions": "Make the bounded maintenance change.",
        "definition_of_done": "Tests pass.",
        "repo_path": "/tmp/repo",
        "branch_name": "bounded-maintenance",
        "estimated_cost_percent": 10,
    }
    values.update(overrides)
    return Task(**values)


def test_scheduler_selects_provider_with_most_safe_headroom(settings) -> None:
    settings.enable_claude_execution = True
    scheduler = Scheduler(settings, None, None, None)

    provider, reason = scheduler.choose_provider(
        task(),
        [snapshot("codex", 55, 60), snapshot("claude", 85, 75)],
    )

    assert provider == "claude"
    assert reason == ""


def test_scheduler_respects_reserves_and_estimated_cost(settings) -> None:
    scheduler = Scheduler(settings, None, None, None)

    provider, reason = scheduler.choose_provider(
        task(estimated_cost_percent=20),
        [snapshot("codex", 35, 70)],
    )

    assert provider is None
    assert "needs 20%" in reason


def test_scheduler_rejects_stale_capacity(settings) -> None:
    scheduler = Scheduler(settings, None, None, None)
    stale = snapshot("codex", 90, 90).model_copy(update={"stale": True})

    provider, reason = scheduler.choose_provider(task(), [stale])

    assert provider is None
    assert reason == "codex unavailable or stale"


def test_scheduler_can_use_a_plan_that_only_exposes_a_weekly_window(settings) -> None:
    scheduler = Scheduler(settings, None, None, None)
    weekly_only = ProviderSnapshot(
        provider_id="codex",
        ready=True,
        source="test",
        windows=[
            UsageWindow(name="weekly", used_percent=10, remaining_percent=90),
        ],
    )

    provider, reason = scheduler.choose_provider(task(), [weekly_only])

    assert provider == "codex"
    assert reason == ""
