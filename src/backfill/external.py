"""Project-scoped admission for processes owned by other applications."""

import hashlib
import json

from pydantic import ValidationError

from backfill.quota import QuotaError
from backfill.schemas import TaskBudget, WorkloadInput
from backfill.tasks import PRIORITY, TaskInput

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_grants (
 id TEXT PRIMARY KEY, project TEXT NOT NULL, hash TEXT NOT NULL UNIQUE,
 revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS external_jobs (
 app TEXT NOT NULL, request TEXT NOT NULL, job TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
 workload TEXT, attempt TEXT, PRIMARY KEY(app, request));
"""


class External:
    def __init__(self, tasks, governor):
        self.tasks, self.governor = tasks, governor
        with tasks.quota.database.transaction() as db:
            db.executescript(SCHEMA)
            if "managed_locally" not in {
                r["name"] for r in db.execute("PRAGMA table_info(app_grants)")
            }:
                db.execute(
                    "ALTER TABLE app_grants ADD COLUMN managed_locally INTEGER NOT NULL DEFAULT 0"
                )

    def sync(self, grants):
        with self.tasks.quota.database.transaction() as db:
            db.execute("UPDATE app_grants SET revoked=1 WHERE managed_locally=0")
            for grant in grants:
                db.execute(
                    "INSERT INTO app_grants(id,project,hash,revoked) VALUES (?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "project=excluded.project,hash=excluded.hash,revoked=excluded.revoked",
                    (grant["id"], grant["project"], grant["hash"], grant["revoked"]),
                )

    def connect_local(self, value):
        project, key, credential = (value.get(k) for k in ("project", "key", "credential"))
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= 100
            or not isinstance(credential, str)
            or not 40 <= len(credential) <= 128
        ):
            raise QuotaError("Invalid project connection", 422)
        if not any(p["id"] == project for p in self.tasks.list()["projects"]):
            raise QuotaError("Project no longer exists", 404)
        ident = "local-" + hashlib.sha256((key + ":" + project).encode()).hexdigest()[:32]
        with self.tasks.quota.database.transaction() as db:
            db.execute(
                "INSERT INTO app_grants(id,project,hash,revoked,managed_locally) "
                "VALUES (?,?,?,0,1) "
                "ON CONFLICT(id) DO UPDATE SET hash=excluded.hash,revoked=0",
                (ident, project, hashlib.sha256(credential.encode()).hexdigest()),
            )
        return {"id": ident, "project": project}

    def identity(self, token):
        with self.tasks.quota.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM app_grants WHERE hash=? AND revoked=0",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if not row:
                raise QuotaError("Project connection is unavailable or revoked", 401)
            return dict(row)

    def register(self, principal, request, value):
        if not isinstance(request, str) or not 8 <= len(request) <= 120:
            raise QuotaError("Stable request ID required", 422)
        try:
            value = TaskInput.model_validate(value)
        except ValidationError as exc:
            raise QuotaError("Invalid task instructions or allowance", 422) from exc
        value.project = principal["project"]
        payload = value.model_dump_json()
        with self.tasks.quota.database.atomic() as db:
            previous = db.execute(
                "SELECT * FROM external_jobs WHERE app=? AND request=?", (principal["id"], request)
            ).fetchone()
            if previous:
                if previous["payload"] != payload:
                    raise QuotaError("Request ID belongs to different instructions", 409)
                self.recover(principal, previous["job"])
                return self.tasks.get(previous["job"])
            job = self.tasks.create(value)
            db.execute(
                "INSERT INTO external_jobs(app,request,job,payload) VALUES (?,?,?,?)",
                (principal["id"], request, job["id"], payload),
            )
            return job

    def record(self, principal, key):
        with self.tasks.quota.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM external_jobs WHERE app=? AND job=?", (principal["id"], key)
            ).fetchone()
            if not row:
                raise QuotaError("Run not found", 404)
            return dict(row)

    def recover(self, principal, key):
        record = self.record(principal, key)
        job = self.tasks.get(key)
        if job["state"] != "running" or self.tasks.now() - job["updated"] < 45:
            return
        self.governor.overview()
        with self.tasks.quota.database.transaction() as db:
            active = db.execute(
                "SELECT 1 FROM native_runs WHERE workload=? AND state='active'",
                (record["workload"],),
            ).fetchone()
        if not active:
            self.tasks.finish(
                key,
                record["attempt"],
                "failed",
                "Runner interrupted. Review saved progress before retrying.",
            )

    def offline(self):
        if not self.tasks.settings.hosted_worker:
            return False
        try:
            seen = float((self.tasks.settings.root / "connection.seen").read_text())
        except (OSError, ValueError):
            seen = 0
        return not 0 <= self.tasks.now() - seen < 45

    def start(self, principal, key):
        with self.tasks.quota.database.atomic() as db:
            self.recover(principal, key)
            job = self.tasks.get(key)
            if job["state"] not in ("queued", "waiting"):
                return {"decision": "wait", "reason": job["state"], "task": job}
            if self.offline():
                return {"decision": "wait", "reason": "Backfill website connection is offline"}
            provider, reason = self.tasks.select(job)
            if not provider:
                db.execute(
                    "UPDATE jobs SET state='waiting',reason=?,updated=? WHERE id=?",
                    (reason, self.tasks.now(), key),
                )
                return {"decision": "wait", "reason": reason}
            attempt = self.tasks.start(key, provider)
            if not attempt:
                return {"decision": "wait", "reason": "Run is already claimed"}
            workload = "ext." + key + "." + provider
            self.tasks.quota.set_workload(
                workload, WorkloadInput(account=provider, priority=PRIORITY[job["priority"]])
            )
            self.governor.set_budget(
                workload,
                TaskBudget(
                    token_limit=10**12,
                    run_token_limit=1000000,
                    max_run_seconds=900,
                    run_cost_usd=30,
                ),
            )
            credential = self.tasks.quota.rotate_token(workload)
            db.execute(
                "UPDATE external_jobs SET workload=?,attempt=? WHERE job=?",
                (workload, attempt["id"], key),
            )
            return {
                "decision": "granted",
                "provider": provider,
                "workload": workload,
                "credential": credential["token"],
                "task": job,
            }

    def enforce(self, workload):
        with self.tasks.quota.database.transaction() as db:
            row = db.execute(
                "SELECT e.*,a.baseline,a.provider,g.revoked FROM external_jobs e "
                "JOIN attempts a ON a.id=e.attempt JOIN app_grants g ON g.id=e.app "
                "WHERE e.workload=?",
                (workload,),
            ).fetchone()
        if not row:
            return
        attempt = {
            "id": row["attempt"],
            "baseline": json.loads(row["baseline"]),
            "provider": row["provider"],
        }
        self.tasks.charge(attempt)
        job = self.tasks.get(row["job"])
        overview = self.tasks.quota.overview()
        observation = next(
            a["observation"] for a in overview["accounts"] if a["key"] == row["provider"]
        )
        own, shared = self.tasks.spending(
            job, row["provider"], observation["windows"] if observation else []
        )
        project = next(
            (p for p in self.tasks.list()["projects"] if p["id"] == job["project"]), None
        )
        prefs = self.tasks.preferences()
        stop = (
            self.offline()
            or row["revoked"]
            or project is None
            or own >= job["allowance"]
            or shared >= project["allowance"]
            or job["state"] in ("paused", "cancelled")
            or prefs.paused
            or (prefs.paused_until and prefs.paused_until.timestamp() > self.tasks.now())
        )
        if stop:
            self.governor.set_budget(
                workload,
                TaskBudget(
                    token_limit=10**12,
                    run_token_limit=1000000,
                    max_run_seconds=900,
                    run_cost_usd=30,
                    paused=True,
                ),
            )

    def update(self, principal, key, body):
        row = self.record(principal, key)
        if not row["attempt"]:
            raise QuotaError("Run has not started", 409)
        self.enforce(row["workload"])
        if isinstance(body.get("output"), str):
            self.tasks.progress(key, row["attempt"], body["output"])
        if body.get("state") in ("review", "failed", "waiting", "paused"):
            with self.tasks.quota.database.transaction() as db:
                active = db.execute(
                    "SELECT 1 FROM native_runs WHERE workload=? AND state='active'",
                    (row["workload"],),
                ).fetchone()
            if active:
                raise QuotaError("Stop the guarded process before finishing", 409)
            self.tasks.finish(
                key, row["attempt"], body["state"], str(body.get("reason", ""))[:2000]
            )
        return self.tasks.get(key)
