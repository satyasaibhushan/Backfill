import asyncio
from contextlib import suppress
from datetime import UTC, datetime

from sqlmodel import Session, select

from backfill.config import Settings
from backfill.models import Run, SystemSetting, Task
from backfill.providers.models import ProviderSnapshot
from backfill.providers.service import ProviderService
from backfill.runner import AgentRunner
from backfill.schemas import TickResult


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        engine,
        providers: ProviderService,
        runner: AgentRunner,
    ):
        self.settings = settings
        self.engine = engine
        self.providers = providers
        self.runner = runner
        self._loop_task: asyncio.Task[None] | None = None
        self._run_tasks: set[asyncio.Task[None]] = set()
        self._tick_lock = asyncio.Lock()

    async def start(self) -> None:
        self._recover_interrupted_runs()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task

    async def tick(self) -> TickResult:
        async with self._tick_lock:
            if not self.settings.scheduler_enabled:
                return TickResult(
                    decision="disabled", reason="Scheduler is disabled in configuration"
                )
            if self.is_paused():
                return TickResult(decision="paused", reason="Scheduler is paused")
            if self.running_count() >= self.settings.max_concurrent_runs:
                return TickResult(decision="busy", reason="Maximum concurrent runs reached")

            snapshots = await self.providers.refresh()
            with Session(self.engine) as session:
                tasks = session.exec(
                    select(Task)
                    .where(Task.status == "queued")
                    .order_by(Task.priority.desc(), Task.created_at.asc())
                ).all()

            if not tasks:
                return TickResult(decision="idle", reason="No queued tasks")

            reasons: list[str] = []
            for task in tasks:
                provider, reason = self.choose_provider(task, snapshots)
                if provider:
                    run_task = asyncio.create_task(self.runner.run(task.id, provider))
                    self._run_tasks.add(run_task)
                    run_task.add_done_callback(self._run_tasks.discard)
                    return TickResult(decision="started", task_id=task.id, provider=provider)
                reasons.append(f"{task.title}: {reason}")
            return TickResult(decision="waiting", reason="; ".join(reasons))

    def choose_provider(
        self, task: Task, snapshots: list[ProviderSnapshot]
    ) -> tuple[str | None, str]:
        allowed = self.settings.execution_providers
        if task.preferred_provider != "auto":
            allowed &= {task.preferred_provider}
        candidates: list[tuple[float, str]] = []
        failures: list[str] = []
        for snapshot in snapshots:
            if snapshot.provider_id not in allowed:
                continue
            if not snapshot.ready or snapshot.stale:
                failures.append(f"{snapshot.provider_id} unavailable or stale")
                continue
            windows = [
                window for name in ("session", "weekly") if (window := snapshot.window(name))
            ]
            if not windows:
                failures.append(f"{snapshot.provider_id} is missing a usable quota window")
                continue
            headroom = []
            for window in windows:
                configured_floor = (
                    self.settings.session_reserve_percent
                    if window.name == "session"
                    else self.settings.weekly_reserve_percent
                )
                task_floor = (
                    task.min_session_remaining
                    if window.name == "session"
                    else task.min_weekly_remaining
                )
                headroom.append(window.remaining_percent - max(configured_floor, task_floor))
            usable = min(headroom)
            if usable < task.estimated_cost_percent:
                failures.append(
                    f"{snapshot.provider_id} has {usable:.0f}% usable headroom, "
                    f"needs {task.estimated_cost_percent:.0f}%"
                )
                continue
            candidates.append((usable - task.estimated_cost_percent, snapshot.provider_id))
        if not candidates:
            return None, ", ".join(failures) or "No enabled execution provider"
        candidates.sort(reverse=True)
        return candidates[0][1], ""

    def set_paused(self, paused: bool) -> None:
        with Session(self.engine) as session:
            setting = session.get(SystemSetting, "scheduler_paused")
            if setting:
                setting.value = "true" if paused else "false"
            else:
                setting = SystemSetting(key="scheduler_paused", value="true" if paused else "false")
            session.add(setting)
            session.commit()

    def is_paused(self) -> bool:
        with Session(self.engine) as session:
            setting = session.get(SystemSetting, "scheduler_paused")
            return setting is not None and setting.value == "true"

    def running_count(self) -> int:
        with Session(self.engine) as session:
            return len(
                session.exec(select(Run).where(Run.status.in_(["starting", "running"]))).all()
            )

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                # A failed cycle must not kill the daemon. The next cycle retries.
                pass
            await asyncio.sleep(self.settings.scheduler_interval_seconds)

    def _recover_interrupted_runs(self) -> None:
        now = datetime.now(UTC)
        with Session(self.engine) as session:
            runs = session.exec(select(Run).where(Run.status.in_(["starting", "running"]))).all()
            for run in runs:
                run.status = "blocked"
                run.error = "Backfill restarted while this run was active"
                run.ended_at = now
                task = session.get(Task, run.task_id)
                if task:
                    task.status = "blocked"
                    task.status_reason = run.error
                    task.updated_at = now
                    session.add(task)
                session.add(run)
            session.commit()
