from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backfill.config import Settings
from backfill.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        auth_mode="dev",
        allowed_login="owner@example.com",
        data_dir=tmp_path / "data",
        scheduler_enabled=False,
    )


@pytest.fixture
def client(settings: Settings) -> Generator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client
