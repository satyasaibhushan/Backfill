from datetime import timedelta

from backfill.config import Settings
from backfill.providers.codex import CodexProvider
from backfill.providers.codexbar import ClaudeCodexBarProvider
from backfill.quota import QuotaError
from backfill.schemas import Observation, Window


async def probe(provider: str, settings: Settings) -> Observation:
    reader = CodexProvider(settings) if provider == "codex" else ClaudeCodexBarProvider(settings)
    snapshot = await reader.probe()
    if not snapshot.ready or not snapshot.account:
        raise QuotaError(snapshot.error or "provider unavailable", 503)
    return Observation(
        observed_at=snapshot.collected_at,
        covered_through=snapshot.collected_at - timedelta(seconds=settings.provider_settle_seconds),
        measurement="estimated",
        source=snapshot.source,
        source_account=snapshot.account,
        windows=[
            Window(
                name=w.name,
                unit="quota_points",
                limit=100,
                used=w.used_percent,
                resets_at=w.resets_at,
                duration_seconds=w.window_minutes * 60,
            )
            for w in snapshot.windows
        ],
    )
