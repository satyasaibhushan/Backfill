from datetime import UTC, datetime

from pydantic import BaseModel, Field


class UsageWindow(BaseModel):
    name: str
    used_percent: float = Field(ge=0, le=100)
    remaining_percent: float = Field(ge=0, le=100)
    window_minutes: int | None = None
    resets_at: datetime | None = None


class ProviderSnapshot(BaseModel):
    provider_id: str
    ready: bool
    source: str
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    account: str | None = None
    plan: str | None = None
    windows: list[UsageWindow] = Field(default_factory=list)
    error: str | None = None
    stale: bool = False

    def window(self, name: str) -> UsageWindow | None:
        return next((window for window in self.windows if window.name == name), None)
