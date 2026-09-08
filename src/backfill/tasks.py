"""Durable task queue. Execution and account metering remain separate modules."""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator

from backfill.config import Settings
from backfill.quota import QuotaError, QuotaService
from backfill.quota_display import FABLE_WINDOW, account_window, duration_label
from backfill.schemas import Contract, Key


class ProjectInput(Contract):
    name: str = Field(min_length=1, max_length=100)
    folder: str = Field(default="", max_length=2000)
    allowance: float = Field(default=10, ge=1, le=50)


class TaskInput(Contract):
    title: str = Field(min_length=1, max_length=160)
    instructions: str = Field(min_length=1, max_length=50000)
    project: Key | None = None
    folder: str = Field(default="", max_length=2000)
    priority: Literal["low", "normal", "high"] = "normal"
    provider: Literal["auto", "codex", "claude"] = "auto"
    allowance: float = Field(default=5, ge=1, le=50)
    schedule: Literal["once", "daily", "weekly"] = "once"
    scheduled_at: AwareDatetime | None = None
    access: Literal["read", "edit"] = "read"
    source_url: str = Field(default="", max_length=2000)

    @field_validator("title", "instructions")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("required")
        return value.strip()

    @field_validator("source_url")
    @classmethod
    def safe_link(cls, value: str) -> str:
        if value and not value.startswith(("https://", "http://")):
            raise ValueError("source must be an HTTP link")
        return value


class TaskAction(Contract):
    action: Literal["pause", "resume", "retry", "approve", "revise", "cancel"]
    until: AwareDatetime | None = None
    feedback: str = Field(default="", max_length=20000)


class Preferences(Contract):
    reserve: int = Field(default=30, ge=5, le=80)
    paused: bool = False
    paused_until: AwareDatetime | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS spaces (id TEXT PRIMARY KEY, config TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, config TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 created REAL NOT NULL, updated REAL NOT NULL, due REAL NOT NULL,
 paused_until REAL, feedback TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
 selected_provider TEXT, output TEXT NOT NULL DEFAULT '', attempt INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attempts (
 id TEXT PRIMARY KEY, job TEXT NOT NULL REFERENCES jobs(id), provider TEXT NOT NULL,
 started REAL NOT NULL, finished REAL, state TEXT NOT NULL, baseline TEXT NOT NULL,
 spending TEXT NOT NULL DEFAULT '{}', output TEXT NOT NULL DEFAULT '',
 reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS app_preferences (
 id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL);
"""
PRIORITY = {"low": 10, "normal": 50, "high": 90}


class Tasks:
    def __init__(self, quota: QuotaService, settings: Settings):
        self.quota = quota
        self.settings = settings
        with quota.database.transaction() as db:
            db.executescript(SCHEMA)
            from backfill.external import SCHEMA as external_schema

            db.executescript(external_schema)
            db.execute(
                "INSERT OR IGNORE INTO app_preferences VALUES (1,?)",
                (Preferences().model_dump_json(),),
            )

    @staticmethod
    def unscheduled(value: TaskInput) -> None:
        if value.schedule != "once" or value.scheduled_at is not None:
            raise QuotaError("Scheduling is not available. Queue a one-time task.", 422)
        if value.source_url:
            value.instructions += "\n\n" + value.source_url
            value.source_url = ""

    @staticmethod
    def consumption(db) -> dict:
        totals, incomplete = {}, set()
        for row in db.execute("SELECT job,provider,baseline,spending,finished FROM attempts"):
            job = row["job"]
            sums = totals.setdefault(job, {})
            baseline = json.loads(row["baseline"])
            charges = json.loads(row["spending"])
            for window in baseline["windows"]:
                if (
                    not account_window(row["provider"], window["name"])
                    and window["name"] != FABLE_WINDOW
                ):
                    continue
                charge = charges.get(window["name"])
                if charge is None:
                    incomplete.add(job)
                    continue
                label = (
                    "Fable weekly"
                    if window["name"] == FABLE_WINDOW
                    else duration_label(window["duration_seconds"])
                )
                key = (row["provider"], label, window["name"])
                used = charge.get("total", charge["used"])
                # Older runs counted the whole window when its reset estimate moved.
                # Recover the delta when the original reset had not actually occurred.
                if (
                    "total" not in charge
                    and charge["reset"] != window["resets_at"]
                    and row["finished"] is not None
                    and datetime.fromisoformat(window["resets_at"]).timestamp() > row["finished"]
                ):
                    used = max(0, used - 100 * window["used"] / window["limit"])
                sums[key] = sums.get(key, 0) + used
        result = {}
        for job, sums in totals.items():
            groups = {}
            for (provider, label, _), used in sums.items():
                key = (provider, label)
                groups[key] = max(groups.get(key, 0), used)
            result[job] = {
                "windows": [
                    {"provider": provider, "label": label, "used": used}
                    for (provider, label), used in sorted(groups.items())
                ],
                "incomplete": job in incomplete,
            }
        return result

    def now(self) -> float:
        return self.quota.clock()

    def preferences(self) -> Preferences:
        with self.quota.database.transaction() as db:
            return Preferences.model_validate_json(
                db.execute("SELECT config FROM app_preferences WHERE id=1").fetchone()[0]
            )

    def set_preferences(self, value: Preferences) -> dict:
        with self.quota.database.transaction() as db:
            db.execute("UPDATE app_preferences SET config=? WHERE id=1", (value.model_dump_json(),))
            from backfill.schemas import Policy

            for row in db.execute("SELECT key,policy FROM accounts").fetchall():
                policy = Policy.model_validate_json(row["policy"])
                policy.cold_start_user_percent = value.reserve
                policy.minimum_user_percent = value.reserve
                policy.priority_percent = min(10, 100 - value.reserve - policy.safety_percent)
                policy.paused = value.paused
                policy.paused_until = value.paused_until
                db.execute(
                    "UPDATE accounts SET policy=? WHERE key=?",
                    (policy.model_dump_json(), row["key"]),
                )
        return value.model_dump(mode="json")

    @staticmethod
    def folder(value: str) -> str:
        if not value:
            return ""
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise QuotaError("This folder does not exist on the execution host.", 422)
        return str(path)

    def project(self, value: ProjectInput, key: str | None = None) -> dict:
        value.folder = self.folder(value.folder)
        if not value.name.strip():
            raise QuotaError("Give the project a name.", 422)
        key = key or secrets.token_hex(8)
        with self.quota.database.transaction() as db:
            if key and db.execute("SELECT 1 FROM spaces WHERE id=?", (key,)).fetchone():
                db.execute("UPDATE spaces SET config=? WHERE id=?", (value.model_dump_json(), key))
            else:
                db.execute("INSERT INTO spaces VALUES (?,?)", (key, value.model_dump_json()))
        return {"id": key, **value.model_dump(mode="json")}

    def create(self, value: TaskInput) -> dict:
        self.unscheduled(value)
        value.folder = self.folder(value.folder)
        with self.quota.database.transaction() as db:
            if value.project:
                row = db.execute(
                    "SELECT config FROM spaces WHERE id=?", (value.project,)
                ).fetchone()
                if not row:
                    raise QuotaError("Project not found.", 404)
                project = ProjectInput.model_validate_json(row[0])
                value.folder = value.folder or project.folder
            key = secrets.token_hex(12)
            now = self.now()
            db.execute(
                "INSERT INTO jobs(id,config,created,updated,due) VALUES (?,?,?,?,?)",
                (
                    key,
                    value.model_dump_json(),
                    now,
                    now,
                    value.scheduled_at.timestamp() if value.scheduled_at else now,
                ),
            )
        return self.get(key)

    @staticmethod
    def _job(db, key):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
        if row is None:
            raise QuotaError("Task not found.", 404)
        return row

    @staticmethod
    def public(row) -> dict:
        return {**dict(row), **json.loads(row["config"]), "config": None}

    def get(self, key: str) -> dict:
        with self.quota.database.transaction() as db:
            result = self.public(self._job(db, key))
            result["attempts"] = [
                dict(r)
                for r in db.execute(
                    "SELECT id,provider,started,finished,state,reason,output FROM "
                    "attempts WHERE job=? ORDER BY started DESC",
                    (key,),
                )
            ]
            result["consumption"] = self.consumption(db).get(
                key, {"windows": [], "incomplete": True}
            )
        return result

    def update(self, key: str, value: TaskInput) -> dict:
        self.unscheduled(value)
        value.folder = self.folder(value.folder)
        with self.quota.database.transaction() as db:
            row = self._job(db, key)
            if row["state"] in ("running", "review", "done", "cancelled"):
                raise QuotaError(
                    "Only pending or paused tasks can be edited. Use feedback to revise a result."
                )
            previous = TaskInput.model_validate_json(row["config"])
            if previous.project != value.project and row["attempt"]:
                raise QuotaError("A task with run history must stay in its original project.")
            if (
                value.project
                and not db.execute("SELECT 1 FROM spaces WHERE id=?", (value.project,)).fetchone()
            ):
                raise QuotaError("Project not found.", 404)
            db.execute(
                "UPDATE jobs SET config=?,updated=?,due=? WHERE id=?",
                (
                    value.model_dump_json(),
                    self.now(),
                    value.scheduled_at.timestamp() if value.scheduled_at else row["due"],
                    key,
                ),
            )
        return self.get(key)

    def action(self, key: str, value: TaskAction) -> dict:
        with self.quota.database.transaction() as db:
            row = self._job(db, key)
            state = row["state"]
            due = row["due"]
            feedback = row["feedback"]
            until = None
            if value.action == "pause":
                if state in ("review", "done", "cancelled"):
                    raise QuotaError("This task has already finished.")
                state = "paused"
                until = value.until.timestamp() if value.until else None
                if until is not None and until <= self.now():
                    raise QuotaError("Choose a future time.", 422)
            elif value.action in ("resume", "retry"):
                if state not in ("paused", "failed", "waiting", "cancelled"):
                    raise QuotaError("This task cannot be resumed in its current state.")
                state, due = "queued", self.now()
            elif value.action == "approve":
                if state != "review":
                    raise QuotaError("Only a completed result can be approved.")
                state = "done"
            elif value.action == "revise":
                if state != "review" or not value.feedback.strip():
                    raise QuotaError("Write feedback on a completed result.", 422)
                feedback = value.feedback.strip()
                state, due = "queued", self.now()
            elif value.action == "cancel":
                state = "cancelled"
            db.execute(
                "UPDATE jobs SET "
                "state=?,due=?,paused_until=?,feedback=?,reason='',updated=? "
                "WHERE id=?",
                (state, due, until, feedback, self.now(), key),
            )
        return self.get(key)

    def recover(self) -> None:
        with self.quota.database.transaction() as db:
            db.execute(
                "UPDATE jobs SET state='failed',reason='Service restarted during "
                "execution. Review the partial result before retrying.' WHERE "
                "state='running'"
            )
            db.execute(
                "UPDATE attempts SET "
                "state='interrupted',finished=?,reason='Service restarted' WHERE "
                "finished IS NULL",
                (self.now(),),
            )

    def list(self) -> dict:
        with self.quota.database.transaction() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created DESC").fetchall()
            usage = self.consumption(db)
            jobs = [
                {
                    **self.public(r),
                    "consumption": usage.get(r["id"], {"windows": [], "incomplete": True}),
                }
                for r in rows
            ]
            projects = [
                {"id": r["id"], **json.loads(r["config"])}
                for r in db.execute("SELECT * FROM spaces ORDER BY rowid")
            ]
        return {
            "tasks": jobs,
            "projects": projects,
            "preferences": self.preferences().model_dump(mode="json"),
        }

    def spending(self, job: dict, account: str, windows: list[dict]) -> tuple[float, float]:
        own = {w["name"]: 0.0 for w in windows}
        shared = dict(own)
        with self.quota.database.transaction() as db:
            rows = db.execute(
                "SELECT a.job,a.spending,j.config FROM attempts a JOIN jobs j ON "
                "j.id=a.job WHERE a.provider=?",
                (account,),
            ).fetchall()
            for row in rows:
                charges = json.loads(row["spending"])
                config = json.loads(row["config"])
                for w in windows:
                    record = charges.get(w["name"])
                    if record and (
                        record["reset"] == w["resets_at"]
                        or datetime.fromisoformat(record["reset"]).timestamp() > self.now()
                    ):
                        if row["job"] == job["id"]:
                            own[w["name"]] += record["used"]
                        if job["project"] and config.get("project") == job["project"]:
                            shared[w["name"]] += record["used"]
        return max(own.values(), default=0), max(shared.values(), default=0)

    def select(self, job: dict) -> tuple[str | None, str]:
        if job["state"] == "waiting" and "approval" in job.get("reason", "").lower():
            return None, job["reason"]
        prefs = self.preferences()
        if prefs.paused or (prefs.paused_until and prefs.paused_until.timestamp() > self.now()):
            return None, "All work is paused."
        overview = self.quota.overview()
        preferred = job["provider"]
        candidates = []
        blockers = []
        for account in overview["accounts"]:
            key = account["key"]
            if key not in ("claude", "codex") or preferred not in ("auto", key):
                continue
            obs = account["observation"]
            with self.quota.database.transaction() as db:
                health = db.execute("SELECT error FROM meters WHERE account=?", (key,)).fetchone()
            if health and health[0]:
                blockers.append(f"{key.title()}: quota reading needs refresh")
                continue
            if (
                not obs
                or self.now() - datetime.fromisoformat(obs["observed_at"]).timestamp()
                > account["policy"]["snapshot_ttl_seconds"]
            ):
                blockers.append(f"{key.title()}: quota reading is stale")
                continue
            policy = account["policy"]
            if policy["paused"] or (
                policy["paused_until"]
                and datetime.fromisoformat(policy["paused_until"]).timestamp() > self.now()
            ):
                continue
            with self.quota.database.transaction() as db:
                from backfill.governor import Governor

                Governor._expire(db, self.now())
                meter = db.execute("SELECT error FROM meters WHERE account=?", (key,)).fetchone()
                busy = db.execute(
                    "SELECT 1 FROM native_runs WHERE provider=? AND state='active'", (key,)
                ).fetchone()
                project = (
                    db.execute("SELECT config FROM spaces WHERE id=?", (job["project"],)).fetchone()
                    if job["project"]
                    else None
                )
            if not meter or meter[0] or busy:
                blockers.append(
                    f"{key.title()}: another task is running"
                    if busy
                    else f"{key.title()}: quota reading unavailable"
                )
                continue
            used, project_used = self.spending(job, key, obs["windows"])
            project_limit = json.loads(project[0])["allowance"] if project else 100
            headroom = min(100 * (w["limit"] - w["used"]) / w["limit"] for w in obs["windows"])
            reserve = (
                max(prefs.reserve, policy["cold_start_user_percent"]) + policy["safety_percent"]
            )
            if headroom <= reserve or used >= job["allowance"] or project_used >= project_limit:
                continue
            candidates.append((headroom - reserve, key))
        if not candidates:
            return (
                None,
                "; ".join(blockers)
                if blockers
                else "Waiting for available capacity. Your personal reserve stays protected.",
            )
        return max(candidates)[1], ""

    def candidates(self) -> list[dict]:
        now = self.now()
        with self.quota.database.transaction() as db:
            db.execute(
                "UPDATE jobs SET state='queued',paused_until=NULL WHERE "
                "state='paused' AND paused_until IS NOT NULL AND paused_until<=?",
                (now,),
            )
            rows = db.execute(
                (
                    "SELECT * FROM jobs WHERE state IN ('queued','waiting') AND "
                    "due<=? AND NOT EXISTS (SELECT 1 FROM external_jobs WHERE "
                    "external_jobs.job=jobs.id)"
                ),
                (now,),
            ).fetchall()
        return sorted(
            (self.public(r) for r in rows),
            key=lambda j: (-PRIORITY[j["priority"]], j["due"], j["created"]),
        )

    def workspace(self, key: str) -> Path:
        root = self.settings.root / "work" / key
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    def start(self, key: str, provider: str) -> dict | None:
        with self.quota.database.transaction() as db:
            row = self._job(db, key)
            if row["state"] not in ("queued", "waiting") or row["due"] > self.now():
                return None
            obs = json.loads(
                db.execute("SELECT observation FROM accounts WHERE key=?", (provider,)).fetchone()[
                    0
                ]
            )
            attempt = secrets.token_hex(12)
            db.execute(
                "INSERT INTO attempts(id,job,provider,started,state,baseline) "
                "VALUES (?,?,?,?,'running',?)",
                (attempt, key, provider, self.now(), json.dumps(obs)),
            )
            db.execute(
                "UPDATE jobs SET "
                "state='running',reason='',selected_provider=?,attempt=attempt+1,u"
                "pdated=? WHERE id=?",
                (provider, self.now(), key),
            )
        return {"id": attempt, "job": key, "provider": provider, "baseline": obs}

    def progress(self, key: str, attempt: str, output: str) -> None:
        output = output[-1000000:]
        with self.quota.database.transaction() as db:
            db.execute("UPDATE jobs SET output=?,updated=? WHERE id=?", (output, self.now(), key))
            db.execute("UPDATE attempts SET output=? WHERE id=?", (output, attempt))

    def charge(self, attempt: dict) -> None:
        overview = self.quota.overview()
        account = next(a for a in overview["accounts"] if a["key"] == attempt["provider"])
        current = account["observation"]
        if not current:
            return
        old = {w["name"]: w for w in attempt["baseline"]["windows"]}
        with self.quota.database.transaction() as db:
            previous = json.loads(
                db.execute("SELECT spending FROM attempts WHERE id=?", (attempt["id"],)).fetchone()[
                    0
                ]
            )
        charges = {}
        for window in current["windows"]:
            base = old.get(window["name"])
            if base:
                used = (
                    max(0, window["used"] - base["used"])
                    if base["resets_at"] == window["resets_at"]
                    or datetime.fromisoformat(base["resets_at"])
                    > datetime.fromisoformat(current["covered_through"])
                    else window["used"]
                )
                used = 100 * used / window["limit"]
                prior = previous.get(window["name"])
                total = used
                if prior:
                    reset_passed = prior["reset"] != window["resets_at"] and datetime.fromisoformat(
                        prior["reset"]
                    ) <= datetime.fromisoformat(current["covered_through"])
                    total = prior.get("total", prior["used"]) + (
                        used if reset_passed else max(0, used - prior["used"])
                    )
                charges[window["name"]] = {
                    "reset": window["resets_at"],
                    "used": used,
                    "total": total,
                }
        with self.quota.database.transaction() as db:
            db.execute(
                "UPDATE attempts SET spending=? WHERE id=?", (json.dumps(charges), attempt["id"])
            )

    def finish(self, key: str, attempt: str, state: str, reason: str = "") -> None:
        with self.quota.database.transaction() as db:
            row = self._job(db, key)
            if row["state"] in ("paused", "cancelled"):
                state = row["state"]
            due = row["due"]
            if state == "waiting":
                due = max(due, self.now() + 60)
                if row["attempt"] >= 3:
                    state = "failed"
                    reason = "Stopped three times. Review the partial result before retrying."
            db.execute(
                "UPDATE jobs SET state=?,reason=?,updated=?,due=? WHERE id=?",
                (state, reason, self.now(), due, key),
            )
            db.execute(
                "UPDATE attempts SET state=?,reason=?,finished=? WHERE id=?",
                (state, reason, self.now(), attempt),
            )
