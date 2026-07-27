from typing import Annotated

from fastapi import APIRouter, Path
from sqlmodel import select

from backfill.auth import IdentityDep
from backfill.database import SessionDep
from backfill.models import Run, RunEvent
from backfill.schemas import RunEventRead, RunRead

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("/")
def list_runs(session: SessionDep, identity: IdentityDep) -> list[RunRead]:
    runs = session.exec(select(Run).order_by(Run.started_at.desc()).limit(100)).all()
    return [RunRead.model_validate(run) for run in runs]


@router.get("/{run_id}/events")
def list_run_events(
    run_id: Annotated[str, Path(min_length=1)],
    session: SessionDep,
    identity: IdentityDep,
) -> list[RunEventRead]:
    events = session.exec(
        select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence.asc())
    ).all()
    return [RunEventRead.model_validate(event) for event in events]
