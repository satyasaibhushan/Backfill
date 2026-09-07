import hashlib
import math
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


class UsageWindow(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    name: str
    used_percent: float = Field(ge=0)
    window_minutes: float = Field(gt=0)
    resets_at: datetime


class ProviderSnapshot(BaseModel):
    provider_id: str
    ready: bool
    source: str
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    account: str | None = None
    windows: list[UsageWindow] = Field(default_factory=list)
    error: str | None = None


def fingerprint(provider: str, account: object) -> str:
    if not isinstance(account, str) or not account.strip():
        raise ValueError("provider did not identify its account")
    return hashlib.sha256(f"{provider}:{account.strip()}".encode()).hexdigest()


def parse_window(name: str, value: object) -> UsageWindow | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid quota window")
    used = value.get("usedPercent")
    if used is None and value.get("remainingPercent") is not None:
        used = 100 - float(value["remainingPercent"])
    duration = value.get("windowDurationMins", value.get("windowMinutes"))
    reset = value.get("resetsAt")
    if used is None or duration is None or reset is None:
        raise ValueError("incomplete quota window")
    if any(isinstance(v, bool) for v in (used, duration, reset)):
        raise ValueError("invalid quota metrics")
    if not all(math.isfinite(float(v)) for v in (used, duration)):
        raise ValueError("nonfinite quota metrics")
    if isinstance(reset, str):
        reset_at = datetime.fromisoformat(reset.replace("Z", "+00:00"))
    else:
        reset_at = datetime.fromtimestamp(float(reset), UTC)
    if reset_at.tzinfo is None:
        raise ValueError("reset timestamp has no timezone")
    return UsageWindow(
        name=name, used_percent=float(used), window_minutes=float(duration), resets_at=reset_at
    )
