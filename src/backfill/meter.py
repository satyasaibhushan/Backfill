import asyncio
from contextlib import suppress

from backfill.config import Settings
from backfill.providers.observe import probe
from backfill.quota import QuotaError, QuotaService
from backfill.schemas import Policy


class Meter:
    def __init__(self, quota: QuotaService, settings: Settings):
        self.quota = quota
        self.settings = settings
        self.lock = asyncio.Lock()

    def bind(self, account: str, provider: str, policy: Policy | None = None) -> None:
        with self.quota.database.transaction() as db:
            old = db.execute("SELECT provider FROM meters WHERE account=?", (account,)).fetchone()
            if old and old[0] != provider:
                raise QuotaError("account provider cannot change")
            duplicate = db.execute(
                "SELECT account FROM meters WHERE provider=? AND account<>?", (provider, account)
            ).fetchone()
            if duplicate:
                raise QuotaError("provider login already bound to another account")
            if policy is None:
                self.quota._account(db, account)
            else:
                db.execute(
                    "INSERT INTO accounts(key,policy) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET policy=excluded.policy",
                    (account, policy.model_dump_json()),
                )
            db.execute(
                "INSERT OR IGNORE INTO meters(account,provider) VALUES (?,?)", (account, provider)
            )

    def defaults(self) -> None:
        for provider in ("codex", "claude"):
            with self.quota.database.transaction() as db:
                found = db.execute("SELECT 1 FROM meters WHERE provider=?", (provider,)).fetchone()
                occupied = db.execute("SELECT 1 FROM accounts WHERE key=?", (provider,)).fetchone()
            if not found and not occupied:
                self.bind(provider, provider, Policy(timezone="Asia/Kolkata"))

    async def refresh(self) -> None:
        async with self.lock:
            with self.quota.database.transaction() as db:
                bindings = [dict(r) for r in db.execute("SELECT * FROM meters")]
            await asyncio.gather(*(self._one(b["account"], b["provider"]) for b in bindings))

    async def _one(self, account: str, provider: str) -> None:
        error = None
        value = None
        try:
            value = await probe(provider, self.settings)
            self.quota.observe(account, value)
        except QuotaError as failure:
            error = failure.message
        except Exception:
            error = "Quota refresh failed. Check the provider login on this host."
        with self.quota.database.transaction() as db:
            # Collector health and admission validity are separate. A rejected quota
            # update must not hide a fresh reading or imply that login is broken.
            db.execute(
                "INSERT INTO meter_readings(account,observation,error) VALUES (?,?,?) "
                "ON CONFLICT(account) DO UPDATE SET observation=excluded.observation, "
                "error=excluded.error",
                (
                    account,
                    value.model_dump_json() if value else None,
                    error if value is None else None,
                ),
            )
            db.execute(
                "UPDATE meters SET checked_at=?,error=? WHERE account=?",
                (self.quota.clock(), error, account),
            )

    async def loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.settings.meter_interval_seconds)

    @staticmethod
    async def stop(task: asyncio.Task) -> None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
