from datetime import UTC, datetime
from uuid import uuid4

from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Task(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    title: str = Field(index=True)
    instructions: str
    definition_of_done: str
    repo_path: str
    branch_name: str
    primary_branch: str = "main"
    upstream_remote: str = "upstream"
    priority: int = Field(default=50, index=True)
    preferred_provider: str = "auto"
    estimated_cost_percent: float = 10
    min_session_remaining: float = 0
    min_weekly_remaining: float = 0
    max_runtime_minutes: int = 60
    status: str = Field(default="queued", index=True)
    status_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Run(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    task_id: str = Field(foreign_key="task.id", index=True)
    provider: str = Field(index=True)
    status: str = Field(default="starting", index=True)
    worktree_path: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    exit_code: int | None = None
    summary: str | None = None
    error: str | None = None


class RunEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(foreign_key="run.id", index=True)
    sequence: int
    kind: str
    message: str
    created_at: datetime = Field(default_factory=utc_now)


class ProviderObservation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    provider_id: str = Field(index=True)
    ready: bool
    source: str
    payload_json: str
    collected_at: datetime = Field(default_factory=utc_now, index=True)


class SystemSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str
