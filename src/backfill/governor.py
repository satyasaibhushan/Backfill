"""Native token budgets. Subscription percentages never masquerade as token counts."""

import json
import secrets
import sqlite3

from backfill.quota import QuotaError, QuotaService
from backfill.schemas import ProjectBudget, RunStart, RunUsage, TaskBudget

HEARTBEAT_SECONDS = 10


def paused(config: ProjectBudget, now: float) -> bool:
    return config.paused or bool(config.paused_until and config.paused_until.timestamp() > now)


class Governor:
    def __init__(self, quota: QuotaService):
        self.quota = quota

    def set_project(self, key: str, value: ProjectBudget) -> dict:
        with self.quota.database.transaction() as db:
            db.execute(
                "INSERT INTO projects VALUES (?,?) ON CONFLICT(key) "
                "DO UPDATE SET config=excluded.config",
                (key, value.model_dump_json()),
            )
        return {"key": key, **value.model_dump(mode="json")}

    def set_budget(self, key: str, value: TaskBudget) -> dict:
        with self.quota.database.transaction() as db:
            self.quota._workload(db, key)
            if (
                value.project
                and not db.execute(
                    "SELECT 1 FROM projects WHERE key=?", (value.project,)
                ).fetchone()
            ):
                raise QuotaError("project not found", 404)
            old = db.execute("SELECT * FROM task_budgets WHERE workload=?", (key,)).fetchone()
            if (
                old
                and old["project"] != value.project
                and db.execute(
                    "SELECT 1 FROM native_runs WHERE workload=? LIMIT 1", (key,)
                ).fetchone()
            ):
                raise QuotaError("a task with recorded usage cannot move projects")
            db.execute(
                "INSERT INTO task_budgets VALUES (?,?,?) ON CONFLICT(workload) DO UPDATE SET "
                "project=excluded.project, config=excluded.config",
                (key, value.project, value.model_dump_json()),
            )
        return {"key": key, **value.model_dump(mode="json")}

    @staticmethod
    def _expire(db: sqlite3.Connection, now: float) -> None:
        # A crashed guard may have spent its entire allowance after its last report.
        db.execute(
            "UPDATE native_runs SET state='uncertain', reason='heartbeat_lost' "
            "WHERE state='active' AND heartbeat_at<=?",
            (now - HEARTBEAT_SECONDS,),
        )

    @staticmethod
    def _charge(row: sqlite3.Row) -> int:
        return (
            max(row["tokens"], row["allocated"])
            if row["state"] in ("active", "uncertain")
            else row["tokens"]
        )

    def _capacity(self, db: sqlite3.Connection, key: str, exclude: str | None = None) -> tuple:
        record = db.execute("SELECT config FROM task_budgets WHERE workload=?", (key,)).fetchone()
        if record is None:
            raise QuotaError("configure a task token budget before starting a guarded process")
        config = TaskBudget.model_validate_json(record["config"])
        rows = db.execute("SELECT * FROM native_runs WHERE workload=?", (key,)).fetchall()
        charged = sum(self._charge(r) for r in rows if r["id"] != exclude)
        remaining = config.token_limit - charged
        reason = "task_paused" if paused(config, self.quota.clock()) else None
        if config.project:
            project = ProjectBudget.model_validate_json(
                db.execute("SELECT config FROM projects WHERE key=?", (config.project,)).fetchone()[
                    0
                ]
            )
            project_rows = db.execute(
                "SELECT r.* FROM native_runs r JOIN task_budgets b ON b.workload=r.workload "
                "WHERE b.project=?",
                (config.project,),
            ).fetchall()
            remaining = min(
                remaining,
                project.token_limit
                - sum(self._charge(r) for r in project_rows if r["id"] != exclude),
            )
            if paused(project, self.quota.clock()):
                reason = "project_paused"
        return config, max(0, remaining), reason

    def _gate(self, db: sqlite3.Connection, key: str) -> str | None:
        workload = self.quota._workload(db, key)
        windows, reason, _ = self.quota._allowances(db, workload, self.quota.clock())
        if reason:
            return reason
        if any(w.available <= 0 for w in windows.values()):
            return "protecting_account_reserve"
        return None

    def start(self, key: str, request: RunStart) -> dict:
        now = self.quota.clock()
        with self.quota.database.transaction() as db:
            self._expire(db, now)
            workload = self.quota._workload(db, key)
            meter = db.execute(
                "SELECT provider FROM meters WHERE account=?", (workload["account"],)
            ).fetchone()
            if meter is None or meter["provider"] != request.provider:
                raise QuotaError("task account is not bound to the requested provider", 422)
            previous = db.execute(
                "SELECT * FROM native_runs WHERE workload=? AND request_id=?",
                (key, request.request_id),
            ).fetchone()
            if previous:
                # Never authorize a second process using a replayed start request.
                return {
                    "decision": "wait",
                    "reason": "request_already_used",
                    "run_id": previous["id"],
                }
            config, remaining, reason = self._capacity(db, key)
            reason = reason or self._gate(db, key)
            if not remaining:
                reason = reason or "token_budget_exhausted"
            active = db.execute(
                "SELECT 1 FROM native_runs r JOIN workloads w ON w.key=r.workload "
                "WHERE w.account=? AND r.state='active'",
                (workload["account"],),
            ).fetchone()
            if active:
                reason = reason or "account_busy"
            if reason:
                return {"decision": "wait", "reason": reason}
            run_id = secrets.token_hex(16)
            db.execute(
                "INSERT INTO native_runs(id,workload,provider,request_id,allocated,"
                "cost_limit,state,"
                "reason,created_at,heartbeat_at,expires_at) "
                "VALUES (?,?,?,?,?,?,'active','allowed',?,?,?)",
                (
                    run_id,
                    key,
                    request.provider,
                    request.request_id,
                    min(remaining, config.run_token_limit),
                    config.run_cost_usd,
                    now,
                    now,
                    now + config.max_run_seconds,
                ),
            )
            response = self._status(db, self._run(db, key, run_id))
            baselines = {}
            for prior in db.execute(
                "SELECT last_report FROM native_runs WHERE workload=? AND provider=? "
                "AND last_report IS NOT NULL ORDER BY created_at",
                (key, request.provider),
            ):
                baselines.update(json.loads(prior[0]).get("counters", {}))
            response["baselines"] = baselines
            return response

    @staticmethod
    def _run(db: sqlite3.Connection, key: str, run_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM native_runs WHERE id=? AND workload=?", (run_id, key)
        ).fetchone()
        if row is None:
            raise QuotaError("run not found", 404)
        return row

    def _status(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict:
        config, remaining, reason = self._capacity(db, row["workload"], row["id"])
        reason = reason or self._gate(db, row["workload"])
        allocation = min(remaining, row["allocated"], config.run_token_limit)
        now = self.quota.clock()
        if row["state"] != "active":
            reason = row["reason"]
        elif now >= min(row["expires_at"], row["created_at"] + config.max_run_seconds):
            reason = "time_limit"
        elif row["tokens"] >= allocation:
            reason = "token_budget_exhausted"
        elif row["provider"] == "claude" and row["cost_usd"] >= min(
            row["cost_limit"], config.run_cost_usd
        ):
            reason = "cost_budget_exhausted"
        return {
            "run_id": row["id"],
            "decision": "wait" if reason else "granted",
            "can_spend": reason is None,
            "reason": reason or "allowed",
            "state": row["state"],
            "token_allowance": allocation,
            "tokens": row["tokens"],
            "cost_usd": row["cost_usd"],
            "cost_limit_usd": min(row["cost_limit"], config.run_cost_usd),
            "expires_at": min(row["expires_at"], row["created_at"] + config.max_run_seconds),
            "heartbeat_seconds": HEARTBEAT_SECONDS,
            "session_id": row["session_id"],
            "overrun_tokens": max(0, row["tokens"] - allocation),
        }

    def report(self, key: str, run_id: str, report: RunUsage) -> dict:
        now = self.quota.clock()
        with self.quota.database.transaction() as db:
            self._expire(db, now)
            row = self._run(db, key, run_id)
            payload = report.model_dump_json()
            if report.sequence == row["sequence"]:
                if payload != row["last_report"]:
                    raise QuotaError("sequence reused with different usage")
                return self._status(db, row)
            if (
                report.sequence < row["sequence"]
                or report.tokens < row["tokens"]
                or report.cost_usd < row["cost_usd"]
            ):
                raise QuotaError("cumulative usage cannot decrease")
            if row["state"] not in ("active", "uncertain"):
                raise QuotaError("run already finalized")
            if row["state"] == "uncertain" and not report.final:
                return self._status(db, row)
            state = "active"
            reason = "allowed"
            if report.final:
                state = "closed" if report.complete else "uncertain"
                reason = report.reason or "interrupted"
            db.execute(
                "UPDATE native_runs SET tokens=?,cost_usd=?,sequence=?,"
                "last_report=?,heartbeat_at=?,"
                "state=?,reason=?,session_id=COALESCE(?,session_id) WHERE id=?",
                (
                    report.tokens,
                    report.cost_usd,
                    report.sequence,
                    payload,
                    now,
                    state,
                    reason,
                    report.session_id,
                    run_id,
                ),
            )
            return self._status(db, self._run(db, key, run_id))

    def status(self, key: str, run_id: str) -> dict:
        with self.quota.database.transaction() as db:
            self._expire(db, self.quota.clock())
            return self._status(db, self._run(db, key, run_id))

    def overview(self) -> dict:
        with self.quota.database.transaction() as db:
            self._expire(db, self.quota.clock())
            runs = db.execute("SELECT * FROM native_runs ORDER BY created_at DESC").fetchall()
            tasks = []
            for row in db.execute("SELECT * FROM task_budgets ORDER BY workload"):
                config, remaining, reason = self._capacity(db, row["workload"])
                owned = [r for r in runs if r["workload"] == row["workload"]]
                tasks.append(
                    {
                        "key": row["workload"],
                        "budget": config.model_dump(mode="json"),
                        "tokens": sum(r["tokens"] for r in owned),
                        "held_tokens": sum(self._charge(r) - r["tokens"] for r in owned),
                        "remaining_tokens": remaining,
                        "reason": reason
                        or self._gate(db, row["workload"])
                        or ("token_budget_exhausted" if not remaining else "allowed"),
                    }
                )
            projects = []
            for row in db.execute("SELECT * FROM projects ORDER BY key"):
                members = [t for t in tasks if t["budget"]["project"] == row["key"]]
                projects.append(
                    {
                        "key": row["key"],
                        **json.loads(row["config"]),
                        "tokens": sum(t["tokens"] for t in members),
                        "held_tokens": sum(t["held_tokens"] for t in members),
                    }
                )
            public_runs = [
                {
                    k: r[k]
                    for k in (
                        "id",
                        "workload",
                        "provider",
                        "allocated",
                        "tokens",
                        "cost_usd",
                        "state",
                        "reason",
                        "created_at",
                        "heartbeat_at",
                        "expires_at",
                        "session_id",
                    )
                }
                for r in runs[:100]
            ]
            meters = [dict(r) for r in db.execute("SELECT * FROM meters ORDER BY account")]
        return {"tasks": tasks, "projects": projects, "runs": public_runs, "meters": meters}
