from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

Key = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")]
Amount = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveAmount = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Policy(Contract):
    cold_start_user_percent: float = Field(default=20, ge=0, le=100)
    minimum_user_percent: float = Field(default=5, ge=0, le=100)
    safety_percent: float = Field(default=5, ge=0, le=100)
    priority_percent: float = Field(default=15, ge=0, le=100)
    forecast_multiplier: float = Field(default=1.25, ge=1, le=5)
    history_min_samples: int = Field(default=12, ge=2, le=1000)
    history_days: int = Field(default=28, ge=1, le=90)
    snapshot_ttl_seconds: int = Field(default=180, ge=1, le=3600)
    max_grant_seconds: int = Field(default=120, ge=1, le=900)
    timezone: str = "UTC"
    paused: bool = False
    paused_until: AwareDatetime | None = None
    reserve_until: AwareDatetime | None = None
    temporary_user_percent: float | None = Field(default=None, ge=0, le=100)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("unknown timezone") from error
        return value

    @model_validator(mode="after")
    def valid_reserves(self) -> "Policy":
        if self.minimum_user_percent > self.cold_start_user_percent:
            raise ValueError("minimum user reserve exceeds cold-start reserve")
        if self.cold_start_user_percent + self.safety_percent + self.priority_percent > 100:
            raise ValueError("reserve percentages exceed 100")
        return self


class AccountInput(Contract):
    policy: Policy = Field(default_factory=Policy)
    provider: Literal["codex", "claude"] | None = None


class Window(Contract):
    name: Key
    unit: Key
    limit: PositiveAmount
    used: Amount
    resets_at: AwareDatetime
    duration_seconds: PositiveAmount


class Observation(Contract):
    observed_at: AwareDatetime
    covered_through: AwareDatetime
    measurement: Literal["measured", "estimated"]
    source: str = Field(min_length=1, max_length=100)
    source_account: str = Field(min_length=1, max_length=128)
    windows: list[Window] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def consistent_windows(self) -> "Observation":
        if self.covered_through > self.observed_at:
            raise ValueError("coverage cannot be later than observation")
        if len({w.name for w in self.windows}) != len(self.windows):
            raise ValueError("window names must be unique")
        if any(w.resets_at <= self.observed_at for w in self.windows):
            raise ValueError("a fresh observation must have future resets")
        return self


class WorkloadInput(Contract):
    account: Key
    priority: int = Field(default=50, ge=0, le=100)
    paused: bool = False


class Acquire(Contract):
    request_id: Key
    costs: dict[Key, Amount] = Field(min_length=1, max_length=32)
    ttl_seconds: int = Field(default=60, ge=1, le=900)

    @model_validator(mode="after")
    def has_cost(self) -> "Acquire":
        if not any(self.costs.values()):
            raise ValueError("at least one window cost must be positive")
        return self


class Report(Contract):
    report_id: Key
    consumed: dict[Key, Amount] = Field(min_length=1, max_length=32)
    final: bool = True


class Allowance(Contract):
    unit: str
    limit: float
    observed_remaining: float
    unconfirmed_usage: float
    reserved: float
    user_reserve: float
    safety_buffer: float
    priority_buffer: float
    available: float
    resets_at: datetime
    forecast_samples: int
    forecast_source: Literal["cold_start", "history"]


class Decision(Contract):
    decision: Literal["granted", "wait"]
    reason: str
    grant_id: str | None = None
    expires_at: datetime | None = None
    retry_at: datetime | None = None
    measurement: Literal["measured", "estimated"] | None = None
    windows: dict[str, Allowance] = Field(default_factory=dict)


class ProjectBudget(Contract):
    token_limit: int = Field(ge=1, le=10**12)
    paused: bool = False
    paused_until: AwareDatetime | None = None


class PauseUntil(Contract):
    paused_until: AwareDatetime | None


class TaskBudget(ProjectBudget):
    project: Key | None = None
    run_token_limit: int = Field(default=100000, ge=1, le=10**9)
    max_run_seconds: int = Field(default=600, ge=1, le=86400)
    run_cost_usd: PositiveAmount = 1


class RunStart(Contract):
    request_id: Key
    provider: Literal["codex", "claude"]


class InferenceUsage(Contract):
    model: str | None = None
    mode: str = "unknown"
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)


class RunUsage(Contract):
    inference: list[InferenceUsage] = Field(default_factory=list, max_length=200)
    sequence: int = Field(ge=0)
    tokens: int = Field(ge=0, le=10**12)
    cost_usd: Amount = 0
    session_id: str | None = Field(default=None, max_length=200)
    final: bool = False
    complete: bool = False
    reason: Literal["completed", "failed", "interrupted", "budget", "unmetered"] | None = None
    counters: dict[Key, Annotated[int, Field(ge=0)]] = Field(default_factory=dict, max_length=200)
