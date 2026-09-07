from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BACKFILL_", extra="ignore")

    data_dir: Path = Path("~/.local/share/backfill")
    provider_timeout_seconds: float = Field(default=45, gt=0, le=120)
    provider_settle_seconds: int = Field(default=120, ge=0, le=3600)
    codex_command: str = "codex"
    codex_task_model: str = "gpt-5.6-luna"
    codex_task_reasoning: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    codexbar_command: str = "codexbar"
    claude_command: str = "claude"
    claude_quota_source: Literal["auto", "oauth", "cli", "web"] | None = None
    hosted_worker: bool = False
    automation_enabled: bool = True
    meter_enabled: bool = True
    meter_interval_seconds: int = Field(default=45, ge=5, le=120)
    dashboard_port: int = Field(default=8431, ge=0, le=65535)

    @property
    def root(self) -> Path:
        return self.data_dir.expanduser().absolute()

    @property
    def socket(self) -> Path:
        return self.root / "quota.sock"


def load_settings() -> Settings:
    return Settings()
