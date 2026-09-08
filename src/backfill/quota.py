import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime

from backfill.database import Database
from backfill.forecast import reserve
from backfill.schemas import (
    Acquire,
    Allowance,
    Decision,
    Observation,
    Policy,
    Report,
    WorkloadInput,
)


def encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def instant(value: float) -> datetime:
    return datetime.fromtimestamp(value, UTC)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class QuotaError(Exception):
    def __init__(self, message: str, status: int = 409):
        self.message = message
        self.status = status
        super().__init__(message)


class QuotaService:
    """One atomic ledger for account observations, reservations, and attributed spending."""

    def __init__(self, database: Database, clock: Callable[[], float] = time.time):
        self.database = database
        self.clock = clock

    @staticmethod
    def _account(db: sqlite3.Connection, key: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM accounts WHERE key=?", (key,)).fetchone()
        if row is None:
            raise QuotaError("account not found", 404)
        return row

    @staticmethod
    def _workload(db: sqlite3.Connection, key: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM workloads WHERE key=?", (key,)).fetchone()
        if row is None:
            raise QuotaError("workload not found", 404)
        return row

    def set_account(self, key: str, policy: Policy) -> dict:
        with self.database.transaction() as db:
            db.execute(
                "INSERT INTO accounts(key,policy) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET policy=excluded.policy",
                (key, policy.model_dump_json()),
            )
        return {"account": key, "policy": policy.model_dump(mode="json")}

    def set_workload(self, key: str, value: WorkloadInput) -> dict:
        with self.database.transaction() as db:
            self._account(db, value.account)
            old = db.execute("SELECT * FROM workloads WHERE key=?", (key,)).fetchone()
            if old and old["account"] != value.account:
                raise QuotaError("a workload cannot move accounts; use a new key")
            token = secrets.token_urlsafe(32) if old is None else None
            db.execute(
                "INSERT INTO workloads(key,account,priority,paused,token_hash) VALUES (?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET priority=excluded.priority, paused=excluded.paused",
                (
                    key,
                    value.account,
                    value.priority,
                    value.paused,
                    token_hash(token) if token else old["token_hash"],
                ),
            )
        return {"key": key, **value.model_dump(), "token": token}

    def rotate_token(self, key: str) -> dict:
        token = secrets.token_urlsafe(32)
        with self.database.transaction() as db:
            self._workload(db, key)
            db.execute("UPDATE workloads SET token_hash=? WHERE key=?", (token_hash(token), key))
        return {"key": key, "token": token}

    def authenticate_worker(self, token: str) -> str | None:
        with self.database.transaction() as db:
            row = db.execute("SELECT key FROM workloads WHERE token_hash=?", (token_hash(token),))
            found = row.fetchone()
        return found["key"] if found else None

    def observe(
        self, key: str, observation: Observation, *, _corrections: frozenset[str] = frozenset()
    ) -> dict:
        now = self.clock()
        if observation.observed_at.timestamp() > now + 2:
            raise QuotaError("observation is in the future", 422)
        if any(w.duration_seconds > 366 * 86400 for w in observation.windows):
            raise QuotaError("window duration exceeds one year", 422)
        if any(w.resets_at.timestamp() - now > w.duration_seconds + 2 for w in observation.windows):
            raise QuotaError("reset exceeds window duration", 422)
        with self.database.transaction() as db:
            account = self._account(db, key)
            duplicate = db.execute(
                "SELECT key FROM accounts WHERE key<>? "
                "AND json_extract(observation, '$.source_account')=?",
                (key, observation.source_account),
            ).fetchone()
            if duplicate:
                raise QuotaError("quota pool already registered under another account key")
            policy = Policy.model_validate_json(account["policy"])
            old = (
                Observation.model_validate_json(account["observation"])
                if account["observation"]
                else None
            )
            if old:
                if old.source_account != observation.source_account:
                    raise QuotaError("source account changed; use a separate account key")
                if observation == old:
                    return {"account": key, "observed": True, "duplicate": True}
                if (
                    observation.observed_at <= old.observed_at
                    or observation.covered_through < old.covered_through
                ):
                    raise QuotaError("observation or coverage moved backwards")
                previous = {w.name: w for w in old.windows}
                current = {w.name: w for w in observation.windows}
                if not previous.keys() <= current.keys():
                    raise QuotaError("observation omitted a known quota window")
                for name, prior in previous.items():
                    window = current[name]
                    if (prior.unit, prior.limit, prior.duration_seconds) != (
                        window.unit,
                        window.limit,
                        window.duration_seconds,
                    ):
                        raise QuotaError("window contract changed; use a separate account key")
                    if name in _corrections:
                        # Repeated native readings can correct usage or an early reset.
                        # Preserve every reservation and debit across the epoch change.
                        self._rebase_window(
                            db, key, name, prior.resets_at.timestamp(), window.resets_at.timestamp()
                        )
                        continue
                    if window.resets_at != prior.resets_at:
                        if prior.resets_at <= observation.covered_through:
                            continue
                        if window.used < prior.used:
                            raise QuotaError("usage decreased before a confirmed reset")
                        # Idle provider windows can move their reset on every poll. A changed
                        # timestamp is not fresh quota: carry all spending and holds with it.
                        self._rebase_window(
                            db, key, name, prior.resets_at.timestamp(), window.resets_at.timestamp()
                        )
                    if window.used < prior.used:
                        raise QuotaError("usage decreased before reset")
                    duration = (observation.covered_through - old.covered_through).total_seconds()
                    if not 60 <= duration <= 21600:
                        continue
                    # Native token counts cannot be subtracted from subscription percentages.
                    # Mixed foreground/background intervals do not train the personal forecast.
                    managed = db.execute(
                        "SELECT 1 FROM native_runs r JOIN workloads w ON w.key=r.workload "
                        "WHERE w.account=? AND r.created_at<=? AND "
                        "(r.state IN ('active','uncertain') OR r.heartbeat_at>=?) LIMIT 1",
                        (
                            key,
                            observation.observed_at.timestamp(),
                            old.covered_through.timestamp() - policy.snapshot_ttl_seconds,
                        ),
                    ).fetchone()
                    if managed:
                        continue
                    attributed = db.execute(
                        "SELECT COALESCE(SUM(amount),0) FROM debits WHERE account=? "
                        "AND window=? AND epoch=? AND estimated=0 AND at>=? AND at<?",
                        (
                            key,
                            name,
                            window.resets_at.timestamp(),
                            old.covered_through.timestamp(),
                            observation.covered_through.timestamp(),
                        ),
                    ).fetchone()[0]
                    # External spending is only an estimate of the human's demand.
                    demand = max(0, window.used - prior.used - attributed)
                    db.execute(
                        "INSERT INTO demand(account,window,unit,capacity,at,duration,amount) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            key,
                            name,
                            window.unit,
                            window.limit,
                            observation.covered_through.timestamp(),
                            duration,
                            demand,
                        ),
                    )
            db.execute(
                "INSERT INTO quota_history VALUES (?,?,?,?)",
                (
                    key,
                    observation.observed_at.timestamp(),
                    observation.covered_through.timestamp(),
                    observation.model_dump_json(),
                ),
            )
            db.execute(
                "UPDATE accounts SET observation=? WHERE key=?",
                (observation.model_dump_json(), key),
            )
            db.execute(
                "DELETE FROM demand WHERE account=? AND at<?",
                (key, now - policy.history_days * 86400),
            )
        return {"account": key, "observed": True, "measurement": observation.measurement}

    @staticmethod
    def _rebase_window(
        db: sqlite3.Connection, account: str, name: str, old_reset: float, new_reset: float
    ) -> None:
        db.execute(
            "UPDATE debits SET epoch=? WHERE account=? AND window=? AND epoch=?",
            (new_reset, account, name, old_reset),
        )
        rows = db.execute(
            "SELECT g.* FROM grants g JOIN workloads w ON w.key=g.workload WHERE w.account=?",
            (account,),
        ).fetchall()
        for row in rows:
            epochs = json.loads(row["epochs"])
            if epochs.get(name) != old_reset:
                continue
            epochs[name] = new_reset
            db.execute(
                "UPDATE grants SET epochs=?,expires_at=? WHERE id=?",
                (encode(epochs), min(row["expires_at"], new_reset), row["id"]),
            )

    def _expire(self, db: sqlite3.Connection, now: float) -> None:
        rows = db.execute(
            "SELECT g.*, w.account FROM grants g JOIN workloads w ON w.key=g.workload "
            "WHERE g.state='active' AND g.expires_at<=?",
            (now,),
        ).fetchall()
        for row in rows:
            costs, consumed, epochs = (json.loads(row[k]) for k in ("costs", "consumed", "epochs"))
            for name, amount in costs.items():
                uncertain = max(0, amount - consumed[name])
                if uncertain:
                    db.execute(
                        "INSERT INTO debits(grant_id,account,window,epoch,amount,at,estimated) "
                        "VALUES (?,?,?,?,?,?,1)",
                        (row["id"], row["account"], name, epochs[name], uncertain, now),
                    )
            db.execute("UPDATE grants SET state='expired' WHERE id=?", (row["id"],))

    def _allowances(
        self, db: sqlite3.Connection, workload: sqlite3.Row, now: float
    ) -> tuple[dict[str, Allowance], str | None, Observation | None]:
        account = self._account(db, workload["account"])
        policy = Policy.model_validate_json(account["policy"])
        observation = (
            Observation.model_validate_json(account["observation"])
            if account["observation"]
            else None
        )
        if observation is None:
            return {}, "missing_observation", None
        reason = None
        meter = db.execute(
            "SELECT error FROM meters WHERE account=?", (workload["account"],)
        ).fetchone()
        if (
            policy.paused
            or workload["paused"]
            or (policy.paused_until and policy.paused_until.timestamp() > now)
        ):
            reason = "paused"
        elif meter and meter["error"]:
            reason = "meter_unavailable"
        elif now - observation.observed_at.timestamp() > policy.snapshot_ttl_seconds:
            reason = "stale_observation"
        elif now - observation.covered_through.timestamp() > policy.snapshot_ttl_seconds * 2:
            reason = "stale_coverage"
        elif any(w.resets_at.timestamp() <= now for w in observation.windows):
            reason = "reset_needs_observation"
        allowances = {}
        active = db.execute(
            "SELECT g.* FROM grants g JOIN workloads w ON w.key=g.workload "
            "WHERE w.account=? AND g.state='active'",
            (workload["account"],),
        ).fetchall()
        for window in observation.windows:
            reset = window.resets_at.timestamp()
            unconfirmed = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM debits WHERE account=? AND window=? "
                "AND epoch=? AND (estimated=1 OR at>=?)",
                (workload["account"], window.name, reset, observation.covered_through.timestamp()),
            ).fetchone()[0]
            reserved = 0.0
            for row in active:
                epochs = json.loads(row["epochs"])
                if epochs.get(window.name) == reset:
                    reserved += max(
                        0,
                        json.loads(row["costs"])[window.name]
                        - json.loads(row["consumed"])[window.name],
                    )
            samples = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM demand WHERE account=? AND window=? AND unit=? "
                    "AND capacity=? AND at>=? ORDER BY at",
                    (
                        workload["account"],
                        window.name,
                        window.unit,
                        window.limit,
                        now - policy.history_days * 86400,
                    ),
                )
            ]
            user, source = reserve(samples, window.limit, now, reset, policy)
            if (
                policy.reserve_until
                and policy.reserve_until.timestamp() > now
                and (policy.temporary_user_percent is not None)
            ):
                user = max(user, window.limit * policy.temporary_user_percent / 100)
            safety = window.limit * policy.safety_percent / 100
            priority = (
                window.limit * policy.priority_percent / 100 * (1 - workload["priority"] / 100)
            )
            remaining = max(0, window.limit - window.used)
            allowances[window.name] = Allowance(
                unit=window.unit,
                limit=window.limit,
                observed_remaining=remaining,
                unconfirmed_usage=unconfirmed,
                reserved=reserved,
                user_reserve=user,
                safety_buffer=safety,
                priority_buffer=priority,
                available=max(0, remaining - unconfirmed - reserved - user - safety - priority),
                resets_at=window.resets_at,
                forecast_samples=len(samples),
                forecast_source=source,
            )
        return allowances, reason, observation

    def acquire(self, key: str, request: Acquire) -> Decision:
        now = self.clock()
        with self.database.transaction() as db:
            workload = self._workload(db, key)
            self._expire(db, now)
            old = db.execute(
                "SELECT * FROM grants WHERE workload=? AND request_id=?", (key, request.request_id)
            ).fetchone()
            if old:
                if json.loads(old["request"]) != request.model_dump():
                    raise QuotaError("request ID already used with different parameters")
                windows, reason, _ = self._allowances(db, workload, now)
                status = self._grant_status(old, windows, reason)
                return Decision(
                    decision="granted" if status["can_spend"] else "wait",
                    reason="existing_grant" if status["can_spend"] else status["reason"],
                    grant_id=old["id"],
                    expires_at=instant(old["expires_at"]),
                )
            windows, reason, observation = self._allowances(db, workload, now)
            if reason:
                return Decision(decision="wait", reason=reason, windows=windows)
            assert observation is not None
            if request.costs.keys() != windows.keys():
                raise QuotaError("costs must include every observed quota window exactly once", 422)
            if any(cost > windows[name].available + 1e-9 for name, cost in request.costs.items()):
                # Quota may become available before reset when observations/reservations change.
                return Decision(
                    decision="wait",
                    reason="insufficient_headroom",
                    windows=windows,
                    measurement=observation.measurement,
                    retry_at=instant(now + 30),
                )
            policy = Policy.model_validate_json(self._account(db, workload["account"])["policy"])
            expires = min(
                now + min(request.ttl_seconds, policy.max_grant_seconds),
                *(w.resets_at.timestamp() for w in observation.windows),
            )
            grant_id = secrets.token_hex(16)
            db.execute(
                "INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    grant_id,
                    key,
                    request.request_id,
                    request.model_dump_json(),
                    encode(request.costs),
                    encode(dict.fromkeys(request.costs, 0)),
                    encode({w.name: w.resets_at.timestamp() for w in observation.windows}),
                    now,
                    expires,
                    "active",
                ),
            )
            return Decision(
                decision="granted",
                reason="within_budget",
                grant_id=grant_id,
                expires_at=instant(expires),
                measurement=observation.measurement,
                windows=windows,
            )

    @staticmethod
    def _grant(db: sqlite3.Connection, key: str, grant_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM grants WHERE id=? AND workload=?", (grant_id, key)
        ).fetchone()
        if row is None:
            raise QuotaError("grant not found", 404)
        return row

    def report(self, key: str, grant_id: str, report: Report) -> dict:
        now = self.clock()
        with self.database.transaction() as db:
            workload = self._workload(db, key)
            self._expire(db, now)
            grant = self._grant(db, key, grant_id)
            prior_report = db.execute(
                "SELECT * FROM reports WHERE grant_id=? AND report_id=?",
                (grant_id, report.report_id),
            ).fetchone()
            if prior_report:
                if json.loads(prior_report["payload"]) != report.model_dump():
                    raise QuotaError("report ID already used with different parameters")
                return json.loads(prior_report["response"])
            costs, consumed, epochs = (
                json.loads(grant[k]) for k in ("costs", "consumed", "epochs")
            )
            if report.consumed.keys() != costs.keys():
                raise QuotaError("report must include every reserved window", 422)
            if any(report.consumed[k] < consumed[k] for k in consumed):
                raise QuotaError("cumulative consumption cannot decrease")
            if grant["state"] in ("closed", "released"):
                raise QuotaError("grant already finalized")
            if grant["state"] == "expired" and not report.final:
                raise QuotaError("an expired grant requires a final report")
            db.execute("DELETE FROM debits WHERE grant_id=? AND estimated=1", (grant_id,))
            for name, amount in report.consumed.items():
                delta = amount - consumed[name]
                if delta:
                    db.execute(
                        "INSERT INTO debits(grant_id,account,window,epoch,amount,at) "
                        "VALUES (?,?,?,?,?,?)",
                        (grant_id, workload["account"], name, epochs[name], delta, now),
                    )
            state = "closed" if report.final else "active"
            db.execute(
                "UPDATE grants SET consumed=?, state=? WHERE id=?",
                (encode(report.consumed), state, grant_id),
            )
            response = {
                "grant_id": grant_id,
                "state": state,
                "consumed": report.consumed,
                "overrun": {
                    name: max(0, amount - costs[name]) for name, amount in report.consumed.items()
                },
            }
            db.execute(
                "INSERT INTO reports VALUES (?,?,?,?)",
                (grant_id, report.report_id, report.model_dump_json(), encode(response)),
            )
            return response

    def release(self, key: str, grant_id: str) -> dict:
        with self.database.transaction() as db:
            self._expire(db, self.clock())
            grant = self._grant(db, key, grant_id)
            if grant["state"] == "expired":
                raise QuotaError("expired spending is uncertain; submit a final usage report")
            if grant["state"] == "active":
                db.execute("UPDATE grants SET state='released' WHERE id=?", (grant_id,))
            return {
                "grant_id": grant_id,
                "state": "released" if grant["state"] == "active" else grant["state"],
            }

    @staticmethod
    def _grant_status(
        grant: sqlite3.Row, windows: dict[str, Allowance], reason: str | None
    ) -> dict:
        epochs = json.loads(grant["epochs"])
        costs = json.loads(grant["costs"])
        if grant["state"] != "active":
            reason = grant["state"]
        elif epochs.keys() != windows.keys() or any(
            epochs[name] != windows[name].resets_at.timestamp() for name in windows
        ):
            reason = "quota_windows_changed"
        elif any(
            w.observed_remaining
            - w.unconfirmed_usage
            - w.reserved
            - w.user_reserve
            - w.safety_buffer
            - w.priority_buffer
            < -1e-9
            for name, w in windows.items()
            if costs.get(name, 0) > 0
        ):
            reason = reason or "headroom_changed"
        remaining = {
            k: max(0, v - json.loads(grant["consumed"])[k])
            for k, v in json.loads(grant["costs"]).items()
        }
        if not any(remaining.values()):
            reason = reason or "reservation_consumed"
        return {
            "grant_id": grant["id"],
            "state": grant["state"],
            "can_spend": reason is None,
            "reason": reason or "within_budget",
            "expires_at": instant(grant["expires_at"]).isoformat(),
            "remaining": remaining,
        }

    def grant_status(self, key: str, grant_id: str) -> dict:
        now = self.clock()
        with self.database.transaction() as db:
            self._expire(db, now)
            workload = self._workload(db, key)
            grant = self._grant(db, key, grant_id)
            windows, reason, _ = self._allowances(db, workload, now)
            return self._grant_status(grant, windows, reason)

    def status(self, key: str) -> dict:
        now = self.clock()
        with self.database.transaction() as db:
            self._expire(db, now)
            workload = self._workload(db, key)
            windows, reason, observation = self._allowances(db, workload, now)
            rows = db.execute(
                "SELECT * FROM grants WHERE workload=? ORDER BY created_at DESC LIMIT 50", (key,)
            ).fetchall()
            return {
                "key": key,
                "account": workload["account"],
                "priority": workload["priority"],
                "paused": bool(workload["paused"]),
                "blocked_reason": reason,
                "measurement": observation.measurement if observation else None,
                "windows": {
                    name: window.model_dump(mode="json") for name, window in windows.items()
                },
                "grants": [self._grant_status(grant, windows, reason) for grant in rows],
            }

    def overview(self) -> dict:
        with self.database.transaction() as db:
            accounts = [
                {
                    "key": row["key"],
                    "policy": json.loads(row["policy"]),
                    "observation": json.loads(row["observation"]) if row["observation"] else None,
                }
                for row in db.execute("SELECT * FROM accounts ORDER BY key")
            ]
            keys = [r["key"] for r in db.execute("SELECT key FROM workloads ORDER BY key")]
        return {"accounts": accounts, "workloads": [self.status(key) for key in keys]}
