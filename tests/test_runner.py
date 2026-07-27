import subprocess
from pathlib import Path

import pytest
from sqlmodel import Session, select

from backfill.database import create_database_engine, initialize_database
from backfill.models import Run, RunEvent, Task
from backfill.runner import AgentRunner


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def repository_with_upstream(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    upstream = tmp_path / "upstream.git"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "Backfill Test")
    git(repository, "config", "user.email", "backfill-test@example.invalid")
    (repository / "README.md").write_text("# Fixture\n")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "Initial fixture")
    git(tmp_path, "init", "--bare", str(upstream))
    git(repository, "remote", "add", "upstream", str(upstream))
    git(repository, "push", "upstream", "main")
    return repository


@pytest.mark.asyncio
async def test_runner_leaves_successful_changes_in_review(settings, tmp_path: Path) -> None:
    repository = repository_with_upstream(tmp_path)
    fake_agent = tmp_path / "fake-agent"
    fake_agent.write_text(
        """#!/usr/bin/env python3
import json
from pathlib import Path
import sys

sys.stdin.read()
Path("runner-output.txt").write_text("done\\n")
print(json.dumps({"type": "item.completed", "item": {"text": "Fixture finished"}}))
"""
    )
    fake_agent.chmod(0o755)
    settings.codex_command = str(fake_agent)
    settings.worktree_dir = tmp_path / "worktrees"
    engine = create_database_engine(settings)
    initialize_database(engine)
    task = Task(
        title="Exercise the isolated runner",
        instructions="Run the bounded fixture agent in its own worktree.",
        definition_of_done="The output is available for review.",
        repo_path=str(repository),
        branch_name="runner-fixture",
        primary_branch="main",
        upstream_remote="upstream",
    )
    with Session(engine) as session:
        session.add(task)
        session.commit()
        session.refresh(task)
        task_id = task.id

    await AgentRunner(settings, engine).run(task_id, "codex")

    with Session(engine) as session:
        stored_task = session.get(Task, task_id)
        run = session.exec(select(Run).where(Run.task_id == task_id)).one()
        events = session.exec(select(RunEvent).where(RunEvent.run_id == run.id)).all()
    assert stored_task.status == "review"
    assert run.status == "review"
    assert run.exit_code == 0
    assert "runner-output.txt" in run.summary
    assert Path(run.worktree_path, "runner-output.txt").read_text() == "done\n"
    assert [event.message for event in events] == ["Fixture finished"]
    assert git(repository, "branch", "--list", "runner-fixture").endswith("runner-fixture")
    engine.dispose()
