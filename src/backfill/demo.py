from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from backfill.database import Database
from backfill.quota import QuotaService
from backfill.schemas import Acquire, Observation, Policy, Report, Window, WorkloadInput


def run_demo() -> list[dict]:
    """Exercise the real ledger with an isolated database and a deterministic clock."""
    with TemporaryDirectory(prefix="backfill-demo-") as directory:
        now = datetime(2026, 9, 7, 9, tzinfo=UTC).timestamp()
        service = QuotaService(Database(Path(directory) / "quota.db"), clock=lambda: now)
        events = []
        service.set_account("personal", Policy(timezone="Asia/Kolkata"))
        for key, priority in (("history-summary", 10), ("task-finder", 90)):
            service.set_workload(key, WorkloadInput(account="personal", priority=priority))
        reset = now + 3600
        weekly_reset = now + 7 * 86400

        def observe(session: float, weekly: float) -> None:
            service.observe(
                "personal",
                Observation(
                    observed_at=datetime.fromtimestamp(now, UTC),
                    covered_through=datetime.fromtimestamp(now - 1, UTC),
                    source="demo",
                    source_account="isolated-demo-account",
                    measurement="estimated",
                    windows=[
                        Window(
                            name=name,
                            unit="quota_points",
                            limit=100,
                            used=used,
                            resets_at=datetime.fromtimestamp(end, UTC),
                            duration_seconds=duration,
                        )
                        for name, used, end, duration in (
                            ("session", session, reset, 18000),
                            ("weekly", weekly, weekly_reset, 7 * 86400),
                        )
                    ],
                ),
            )

        def request(key: str, request_id: str, session: float, weekly: float = 1):
            result = service.acquire(
                key, Acquire(request_id=request_id, costs={"session": session, "weekly": weekly})
            )
            events.append(
                {
                    "step": request_id,
                    "workload": key,
                    "decision": result.decision,
                    "reason": result.reason,
                    "available": {k: round(w.available, 2) for k, w in result.windows.items()},
                }
            )
            return result

        observe(40, 10)
        events.append(
            {
                "step": "start",
                "mode": "isolated simulation using the production ledger",
                "reserve": "20% user + 5% safety + priority buffer; each window independent",
            }
        )
        bulk = request("history-summary", "spare-capacity", 20)
        assert bulk.decision == "granted" and bulk.grant_id
        low = request("history-summary", "competing-low-priority", 5)
        high = request("task-finder", "important-work", 5)
        assert low.decision == "wait" and high.decision == "granted" and high.grant_id
        service.report(
            "history-summary",
            bulk.grant_id,
            Report(report_id="done", consumed={"session": 12, "weekly": 1}),
        )
        service.release("task-finder", high.grant_id)
        now += 30
        observe(70, 12)
        assert request("history-summary", "user-starts-working", 5).decision == "wait"
        important = request("task-finder", "important-still-fits", 2)
        assert important.grant_id
        service.release("task-finder", important.grant_id)
        now += 200
        assert request("task-finder", "stale-reader", 1).reason == "stale_observation"
        now = reset + 1
        reset += 18000
        observe(0, 80)
        assert request("task-finder", "weekly-still-exhausted", 1).decision == "wait"
        now = weekly_reset + 1
        reset = now + 18000
        weekly_reset = now + 7 * 86400
        observe(0, 0)
        resumed = request("history-summary", "fresh-reset", 20)
        assert resumed.grant_id
        service.release("history-summary", resumed.grant_id)
        service.set_account("personal", Policy(paused=True))
        assert request("task-finder", "owner-pauses-account", 1).reason == "paused"

        # Quiet versus busy histories go through the same public observation path.
        for label, hourly in (("quiet-history", 1), ("busy-history", 8)):
            service.set_account(label, Policy())
            service.set_workload(label, WorkloadInput(account=label, priority=50))
            for day in range(4):
                end = datetime.fromtimestamp(now, UTC) - timedelta(days=4 - day)
                end = end.replace(hour=14, minute=0, second=0)
                for hour in range(5):
                    at = end - timedelta(hours=5 - hour)
                    service.observe(
                        label,
                        Observation(
                            observed_at=at,
                            covered_through=at,
                            source="demo",
                            source_account=label,
                            measurement="measured",
                            windows=[
                                Window(
                                    name="session",
                                    unit="quota_points",
                                    limit=100,
                                    used=hour * hourly,
                                    resets_at=end,
                                    duration_seconds=18000,
                                )
                            ],
                        ),
                    )
            at = datetime.fromtimestamp(now, UTC)
            service.observe(
                label,
                Observation(
                    observed_at=at,
                    covered_through=at,
                    source="demo",
                    source_account=label,
                    measurement="measured",
                    windows=[
                        Window(
                            name="session",
                            unit="quota_points",
                            limit=100,
                            used=0,
                            resets_at=at + timedelta(hours=5),
                            duration_seconds=18000,
                        )
                    ],
                ),
            )
            window = service.status(label)["windows"]["session"]
            events.append(
                {
                    "step": label,
                    "forecast": window["forecast_source"],
                    "samples": window["forecast_samples"],
                    "user_reserve": window["user_reserve"],
                    "available": window["available"],
                }
            )
        events.append({"step": "complete", "assertions": "passed", "provider_requests": 0})
        return events
