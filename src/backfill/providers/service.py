import asyncio
import json
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from backfill.config import Settings
from backfill.models import ProviderObservation
from backfill.providers.codex import CodexProvider
from backfill.providers.codexbar import ClaudeCodexBarProvider
from backfill.providers.models import ProviderSnapshot


class ProviderService:
    def __init__(self, settings: Settings, engine):
        self.settings = settings
        self.engine = engine
        self.providers = [CodexProvider(settings), ClaudeCodexBarProvider(settings)]
        self._latest: dict[str, ProviderSnapshot] = {}
        self._lock = asyncio.Lock()

    async def refresh(self, *, force: bool = False) -> list[ProviderSnapshot]:
        async with self._lock:
            if not force and self._cache_fresh():
                return list(self._latest.values())
            results = await asyncio.gather(*(provider.probe() for provider in self.providers))
            now = datetime.now(UTC)
            with Session(self.engine) as session:
                for result in results:
                    previous = self._latest.get(result.provider_id)
                    if not result.ready and previous and previous.ready:
                        previous_age = now - previous.collected_at
                        if previous_age <= timedelta(minutes=10):
                            result = previous.model_copy(
                                update={
                                    "stale": True,
                                    "error": result.error,
                                }
                            )
                    self._latest[result.provider_id] = result
                    session.add(
                        ProviderObservation(
                            provider_id=result.provider_id,
                            ready=result.ready,
                            source=result.source,
                            payload_json=result.model_dump_json(),
                            collected_at=now,
                        )
                    )
                session.commit()
            return list(self._latest.values())

    def latest(self) -> list[ProviderSnapshot]:
        if self._latest:
            return list(self._latest.values())
        with Session(self.engine) as session:
            observations = session.exec(
                select(ProviderObservation).order_by(ProviderObservation.collected_at.desc())
            ).all()
        for observation in observations:
            if observation.provider_id not in self._latest:
                payload = json.loads(observation.payload_json)
                self._latest[observation.provider_id] = ProviderSnapshot.model_validate(payload)
        return list(self._latest.values())

    def _cache_fresh(self) -> bool:
        if len(self._latest) < len(self.providers):
            return False
        cutoff = datetime.now(UTC) - timedelta(seconds=self.settings.provider_refresh_seconds)
        return all(snapshot.collected_at >= cutoff for snapshot in self._latest.values())
