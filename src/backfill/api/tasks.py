from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, status
from sqlmodel import select

from backfill.auth import IdentityDep, MutationIdentityDep
from backfill.database import SessionDep
from backfill.models import Task
from backfill.schemas import TaskCreate, TaskPatch, TaskRead, slugify
from backfill.worktrees import WorktreeError, validate_branch_name

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/")
def list_tasks(session: SessionDep, identity: IdentityDep) -> list[TaskRead]:
    tasks = session.exec(select(Task).order_by(Task.priority.desc(), Task.created_at.asc())).all()
    return [TaskRead.model_validate(task) for task in tasks]


@router.post("/", status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskCreate,
    session: SessionDep,
    identity: MutationIdentityDep,
) -> TaskRead:
    branch_name = payload.branch_name or slugify(payload.title)
    try:
        validate_branch_name(branch_name)
    except WorktreeError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    task = Task(**payload.model_dump(exclude={"branch_name"}), branch_name=branch_name)
    session.add(task)
    session.commit()
    session.refresh(task)
    return TaskRead.model_validate(task)


@router.patch("/{task_id}")
def update_task(
    task_id: Annotated[str, Path(min_length=1)],
    payload: TaskPatch,
    session: SessionDep,
    identity: MutationIdentityDep,
) -> TaskRead:
    task = _task_or_404(session, task_id)
    if task.status == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Running tasks cannot be edited"
        )
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    task.updated_at = datetime.now(UTC)
    session.add(task)
    session.commit()
    session.refresh(task)
    return TaskRead.model_validate(task)


@router.post("/{task_id}/actions/{action}")
async def task_action(
    request: Request,
    task_id: Annotated[str, Path(min_length=1)],
    action: Annotated[str, Path(pattern="^(queue|pause|cancel|retry)$")],
    session: SessionDep,
    identity: MutationIdentityDep,
) -> TaskRead:
    task = _task_or_404(session, task_id)
    if action in {"queue", "retry"}:
        if task.status == "running":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task is running")
        task.status = "queued"
        task.status_reason = None
    elif action == "pause":
        task.status = "paused"
        task.status_reason = "Paused by owner"
        await request.app.state.runner.stop_task(task.id)
    elif action == "cancel":
        task.status = "cancelled"
        task.status_reason = "Cancelled by owner"
        await request.app.state.runner.stop_task(task.id)
    task.updated_at = datetime.now(UTC)
    session.add(task)
    session.commit()
    session.refresh(task)
    return TaskRead.model_validate(task)


def _task_or_404(session, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task
