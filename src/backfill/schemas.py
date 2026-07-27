from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ProviderChoice = Literal["auto", "codex", "claude"]
TaskStatus = Literal["queued", "running", "paused", "review", "blocked", "failed", "cancelled"]


def slugify(value: str) -> str:
    normalized = "".join(character.lower() if character.isalnum() else "-" for character in value)
    return "-".join(part for part in normalized.split("-") if part)[:48] or "task"


class TaskCreate(BaseModel):
    title: str = Field(min_length=3, max_length=160)
    instructions: str = Field(min_length=10, max_length=20_000)
    definition_of_done: str = Field(min_length=3, max_length=10_000)
    repo_path: str = Field(min_length=1, max_length=1_000)
    branch_name: str | None = Field(default=None, max_length=120)
    primary_branch: str = Field(default="main", min_length=1, max_length=120)
    upstream_remote: str = Field(default="upstream", min_length=1, max_length=120)
    priority: int = Field(default=50, ge=0, le=100)
    preferred_provider: ProviderChoice = "auto"
    estimated_cost_percent: float = Field(default=10, gt=0, le=100)
    min_session_remaining: float = Field(default=0, ge=0, le=100)
    min_weekly_remaining: float = Field(default=0, ge=0, le=100)
    max_runtime_minutes: int = Field(default=60, ge=5, le=720)

    @field_validator("repo_path")
    @classmethod
    def normalize_repo_path(cls, value: str) -> str:
        return str(Path(value).expanduser().resolve())


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=160)
    instructions: str | None = Field(default=None, min_length=10, max_length=20_000)
    definition_of_done: str | None = Field(default=None, min_length=3, max_length=10_000)
    priority: int | None = Field(default=None, ge=0, le=100)
    preferred_provider: ProviderChoice | None = None
    estimated_cost_percent: float | None = Field(default=None, gt=0, le=100)
    min_session_remaining: float | None = Field(default=None, ge=0, le=100)
    min_weekly_remaining: float | None = Field(default=None, ge=0, le=100)
    max_runtime_minutes: int | None = Field(default=None, ge=5, le=720)


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    instructions: str
    definition_of_done: str
    repo_path: str
    branch_name: str
    primary_branch: str
    upstream_remote: str
    priority: int
    preferred_provider: str
    estimated_cost_percent: float
    min_session_remaining: float
    min_weekly_remaining: float
    max_runtime_minutes: int
    status: str
    status_reason: str | None
    created_at: datetime
    updated_at: datetime


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    provider: str
    status: str
    worktree_path: str | None
    started_at: datetime
    ended_at: datetime | None
    exit_code: int | None
    summary: str | None
    error: str | None


class RunEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: str
    sequence: int
    kind: str
    message: str
    created_at: datetime


class ControlRead(BaseModel):
    scheduler_enabled: bool
    paused: bool
    running_count: int
    execution_providers: list[str]


class TickResult(BaseModel):
    decision: str
    task_id: str | None = None
    provider: str | None = None
    reason: str | None = None
