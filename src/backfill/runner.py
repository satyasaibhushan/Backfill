import asyncio
import json
import shutil
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from backfill.config import Settings
from backfill.models import Run, RunEvent, Task
from backfill.worktrees import WorktreeError, WorktreeManager


class AgentRunner:
    def __init__(self, settings: Settings, engine):
        self.settings = settings
        self.engine = engine
        self.worktrees = WorktreeManager(settings)
        self.processes: dict[str, asyncio.subprocess.Process] = {}

    async def run(self, task_id: str, provider: str) -> None:
        with Session(self.engine) as session:
            task = session.get(Task, task_id)
            if not task or task.status != "queued":
                return
            task.status = "running"
            task.status_reason = f"Preparing isolated worktree for {provider}"
            task.updated_at = datetime.now(UTC)
            run = Run(task_id=task.id, provider=provider)
            session.add(task)
            session.add(run)
            session.commit()
            session.refresh(run)
            session.refresh(task)
            task_snapshot = Task(**task.model_dump())
            task_id = task.id
            run_id = run.id

        try:
            worktree = await self.worktrees.prepare(task_snapshot)
        except WorktreeError as error:
            self._finish_blocked(task_id, run_id, str(error))
            return

        with Session(self.engine) as session:
            stored_run = session.get(Run, run_id)
            stored_task = session.get(Task, task_id)
            if not stored_run or not stored_task:
                return
            stored_run.worktree_path = str(worktree)
            stored_run.status = "running"
            stored_task.status_reason = f"{provider} is working in {worktree}"
            session.add(stored_run)
            session.add(stored_task)
            session.commit()

        prompt = self._prompt(task_snapshot)
        try:
            command = self._command(provider, worktree)
        except RuntimeError as error:
            self._finish_blocked(task_id, run_id, str(error))
            return

        sequence = 0
        sequence_lock = asyncio.Lock()

        async def record(kind: str, message: str) -> None:
            nonlocal sequence
            cleaned = message.strip()
            if not cleaned:
                return
            async with sequence_lock:
                sequence += 1
                current = sequence
            with Session(self.engine) as event_session:
                event_session.add(
                    RunEvent(
                        run_id=run_id,
                        sequence=current,
                        kind=kind,
                        message=cleaned[:20_000],
                    )
                )
                event_session.commit()

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=worktree,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.processes[run_id] = process
            if not process.stdin:
                raise RuntimeError("Agent stdin is unavailable")
            process.stdin.write(prompt.encode())
            await process.stdin.drain()
            process.stdin.close()

            async def consume(stream: asyncio.StreamReader | None, kind: str) -> None:
                if not stream:
                    return
                while line := await stream.readline():
                    text = line.decode(errors="replace")
                    await record(kind, self._event_message(text))

            stdout_task = asyncio.create_task(consume(process.stdout, "agent"))
            stderr_task = asyncio.create_task(consume(process.stderr, "stderr"))
            try:
                async with asyncio.timeout(task_snapshot.max_runtime_minutes * 60):
                    exit_code = await process.wait()
            except TimeoutError:
                process.terminate()
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=10)
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                await record("system", "Run exceeded its configured time limit.")
                exit_code = 124
            await asyncio.gather(stdout_task, stderr_task)
            summary = await self.worktrees.summary(worktree)
            self._finish(task_id, run_id, exit_code, summary)
        except (OSError, RuntimeError) as error:
            await record("system", str(error))
            self._finish(task_id, run_id, 1, "", error=str(error))
        finally:
            self.processes.pop(run_id, None)
            if process and process.returncode is None:
                process.kill()
                await process.wait()

    async def stop_task(self, task_id: str) -> bool:
        with Session(self.engine) as session:
            running = session.exec(
                select(Run)
                .where(Run.task_id == task_id)
                .where(Run.status.in_(["starting", "running"]))
                .order_by(Run.started_at.desc())
            ).first()
        if not running:
            return False
        process = self.processes.get(running.id)
        if process and process.returncode is None:
            process.terminate()
            return True
        return False

    def _command(self, provider: str, worktree: Path) -> list[str]:
        if provider == "codex":
            if not self.settings.enable_codex_execution:
                raise RuntimeError("Codex execution is disabled")
            if not shutil.which(self.settings.codex_command):
                raise RuntimeError(f"{self.settings.codex_command} is not installed")
            return [
                self.settings.codex_command,
                "exec",
                "--json",
                "-s",
                "workspace-write",
                "-a",
                "never",
                "-C",
                str(worktree),
                "-",
            ]
        if provider == "claude":
            if not self.settings.enable_claude_execution:
                raise RuntimeError(
                    "Claude execution is disabled until its Devbox permission policy is reviewed"
                )
            if not shutil.which(self.settings.claude_command):
                raise RuntimeError(f"{self.settings.claude_command} is not installed")
            return [
                self.settings.claude_command,
                "-p",
                "--output-format",
                "stream-json",
                "--permission-mode",
                "acceptEdits",
            ]
        raise RuntimeError(f"Unsupported execution provider: {provider}")

    @staticmethod
    def _prompt(task: Task) -> str:
        return f"""You are working on one bounded Backfill job in an isolated git worktree.

Task: {task.title}

Instructions:
{task.instructions}

Definition of done:
{task.definition_of_done}

Operating constraints:
- Inspect repository instructions before changing code.
- Keep the solution concise and preserve unrelated behavior.
- Run relevant tests and verification.
- Do not commit, push, open a pull request, or merge.
- Never add AI attribution to code, branches, messages, or generated files.
- If blocked, stop and clearly report the exact blocker.
- Leave all completed changes in the current worktree for human review.
"""

    @staticmethod
    def _event_message(line: str) -> str:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return line
        event_type = payload.get("type") or payload.get("method") or "event"
        if event_type == "item.completed":
            item = payload.get("item") or {}
            return str(item.get("text") or item.get("type") or event_type)
        if event_type == "result":
            return str(payload.get("result") or payload.get("subtype") or event_type)
        return json.dumps(payload, separators=(",", ":"))

    def _finish_blocked(self, task_id: str, run_id: str, reason: str) -> None:
        with Session(self.engine) as session:
            task = session.get(Task, task_id)
            run = session.get(Run, run_id)
            if task:
                task.status = "blocked"
                task.status_reason = reason
                task.updated_at = datetime.now(UTC)
                session.add(task)
            if run:
                run.status = "blocked"
                run.error = reason
                run.ended_at = datetime.now(UTC)
                session.add(run)
            session.commit()

    def _finish(
        self,
        task_id: str,
        run_id: str,
        exit_code: int,
        summary: str,
        *,
        error: str | None = None,
    ) -> None:
        with Session(self.engine) as session:
            task = session.get(Task, task_id)
            run = session.get(Run, run_id)
            if not task or not run:
                return
            requested_state = task.status if task.status in {"paused", "cancelled"} else None
            if requested_state:
                final_status = requested_state
            else:
                final_status = "review" if exit_code == 0 else "failed"
            task.status = final_status
            task.status_reason = (
                "Changes are ready for review"
                if final_status == "review"
                else error or f"Agent exited with status {exit_code}"
            )
            task.updated_at = datetime.now(UTC)
            run.status = final_status
            run.exit_code = exit_code
            run.summary = summary
            run.error = error
            run.ended_at = datetime.now(UTC)
            session.add(task)
            session.add(run)
            session.commit()
