from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BACKFILL_",
        env_file=".env",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8430
    data_dir: Path = Path("~/.local/share/backfill")
    worktree_dir: Path | None = None

    auth_mode: Literal["tailscale", "dev"] = "tailscale"
    allowed_login: str | None = None
    public_origin: str | None = None

    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = 60
    provider_refresh_seconds: int = 60
    provider_timeout_seconds: int = 20
    session_reserve_percent: float = 20
    weekly_reserve_percent: float = 15
    max_concurrent_runs: int = 1

    codex_command: str = "codex"
    codexbar_command: str = "codexbar"
    claude_command: str = "claude"
    enable_codex_execution: bool = True
    enable_claude_execution: bool = False

    @field_validator("allowed_login")
    @classmethod
    def normalize_login(cls, value: str | None) -> str | None:
        return value.strip().lower() if value else None

    @field_validator("data_dir", "worktree_dir", mode="before")
    @classmethod
    def expand_path(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        return value

    @property
    def resolved_data_dir(self) -> Path:
        return self.data_dir.expanduser().resolve()

    @property
    def resolved_worktree_dir(self) -> Path:
        configured = self.worktree_dir or self.resolved_data_dir / "worktrees"
        return configured.expanduser().resolve()

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.resolved_data_dir / 'backfill.db'}"

    @property
    def auth_configured(self) -> bool:
        if self.auth_mode == "dev":
            return self.allowed_login is not None and self.host in {
                "127.0.0.1",
                "localhost",
                "::1",
            }
        return bool(
            self.allowed_login
            and self.public_origin
            and self.host in {"127.0.0.1", "localhost", "::1"}
        )

    @property
    def execution_providers(self) -> set[str]:
        providers: set[str] = set()
        if self.enable_codex_execution:
            providers.add("codex")
        if self.enable_claude_execution:
            providers.add("claude")
        return providers


@lru_cache
def load_settings() -> Settings:
    return Settings()
