from collections.abc import Generator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from sqlmodel import Session, SQLModel, create_engine

from backfill.config import Settings


def create_database_engine(settings: Settings):
    Path(settings.resolved_data_dir).mkdir(parents=True, exist_ok=True)
    return create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
    )


def initialize_database(engine) -> None:
    SQLModel.metadata.create_all(engine)


def get_session(request: Request) -> Generator[Session]:
    with Session(request.app.state.engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
