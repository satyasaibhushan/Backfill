from datetime import UTC, datetime, timedelta

import pytest

from backfill.database import create_database_engine, initialize_database
from backfill.providers.codex import CodexProvider
from backfill.providers.codexbar import ClaudeCodexBarProvider
from backfill.providers.models import ProviderSnapshot, UsageWindow
from backfill.providers.service import ProviderService


def test_codex_rate_limit_response_is_normalized(settings) -> None:
    provider = CodexProvider(settings)

    snapshot = provider._parse(
        {"account": {"email": "owner@example.com", "planType": "plus"}},
        {
            "rateLimits": {
                "primary": {
                    "usedPercent": 25,
                    "windowDurationMins": 300,
                    "resetsAt": 1_800_000_000,
                },
                "secondary": {
                    "usedPercent": 40,
                    "windowDurationMins": 10_080,
                    "resetsAt": 1_800_100_000,
                },
            }
        },
    )

    assert snapshot.ready is True
    assert snapshot.window("session").remaining_percent == 75
    assert snapshot.window("weekly").remaining_percent == 60
    assert snapshot.plan == "plus"


def test_codex_weekly_only_window_is_named_by_duration(settings) -> None:
    provider = CodexProvider(settings)

    snapshot = provider._parse(
        {"account": {"email": "owner@example.com", "planType": "plus"}},
        {
            "rateLimits": {
                "primary": {
                    "usedPercent": 9,
                    "windowDurationMins": 10_080,
                    "resetsAt": 1_800_000_000,
                },
                "secondary": None,
            }
        },
    )

    assert snapshot.window("session") is None
    assert snapshot.window("weekly").remaining_percent == 91


def test_codexbar_claude_response_is_normalized(settings) -> None:
    provider = ClaudeCodexBarProvider(settings)

    snapshot = provider._parse(
        {
            "provider": "claude",
            "source": "oauth",
            "usage": {
                "primary": {"usedPercent": 10, "windowDurationMins": 300},
                "secondary": {"remainingPercent": 55, "windowDurationMins": 10_080},
            },
        }
    )

    assert snapshot.ready is True
    assert snapshot.window("session").remaining_percent == 90
    assert snapshot.window("weekly").used_percent == 45


class SequenceProvider:
    provider_id = "codex"

    def __init__(self, snapshots: list[ProviderSnapshot]):
        self.snapshots = iter(snapshots)

    async def probe(self) -> ProviderSnapshot:
        return next(self.snapshots)


@pytest.mark.asyncio
async def test_provider_failure_keeps_last_good_sample_without_extending_it(settings) -> None:
    engine = create_database_engine(settings)
    initialize_database(engine)
    service = ProviderService(settings, engine)
    collected_at = datetime.now(UTC) - timedelta(minutes=2)
    good = ProviderSnapshot(
        provider_id="codex",
        ready=True,
        source="test",
        collected_at=collected_at,
        windows=[
            UsageWindow(name="session", used_percent=20, remaining_percent=80),
            UsageWindow(name="weekly", used_percent=30, remaining_percent=70),
        ],
    )
    failure = ProviderSnapshot(
        provider_id="codex",
        ready=False,
        source="test",
        error="temporary failure",
    )
    service.providers = [SequenceProvider([good, failure])]

    await service.refresh(force=True)
    snapshots = await service.refresh(force=True)

    assert snapshots[0].ready is True
    assert snapshots[0].stale is True
    assert snapshots[0].error == "temporary failure"
    assert snapshots[0].collected_at == collected_at
    engine.dispose()
