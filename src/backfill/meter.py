import asyncio
from contextlib import suppress

from backfill.config import Settings
from backfill.providers.observe import probe
from backfill.quota import QuotaError, QuotaService
from backfill.schemas import Observation, Policy


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
            if value and error in {
                "usage decreased before a confirmed reset",
                "usage decreased before reset",
            }:
                corrections = self._confirm_correction(account, value)
                if corrections:
                    try:
                        self.quota.observe(account, value, _corrections=corrections)
                        error = None
                    except QuotaError as rejected:
                        error = rejected.message
        except Exception:
            error = "Quota refresh failed. Check the provider login on this host."
        if error not in {
            "usage decreased before a confirmed reset",
            "usage decreased before reset",
        }:
            with self.quota.database.transaction() as db:
                db.execute("DELETE FROM meter_corrections WHERE account=?", (account,))
        if error == "observation omitted a known quota window":
            with self.quota.database.transaction() as db:
                row = db.execute(
                    "SELECT observation,policy FROM accounts WHERE key=?", (account,)
                ).fetchone()
            if row and row["observation"]:
                previous = Observation.model_validate_json(row["observation"])
                policy = Policy.model_validate_json(row["policy"])
                now = self.quota.clock()
                # A partial sample does not invalidate a still-fresh complete sample.
                # Keep its original timestamps; never extend its admission lifetime.
                if (
                    now - previous.observed_at.timestamp() <= policy.snapshot_ttl_seconds
                    and now - previous.covered_through.timestamp()
                    <= policy.snapshot_ttl_seconds * 2
                    and all(w.resets_at.timestamp() > now for w in previous.windows)
                ):
                    value, error = previous, None
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

    def _confirm_correction(self, account: str, value: Observation) -> frozenset[str]:
        if value.source not in {"codex-app-server", "codexbar"}:
            return frozenset()
        now = self.quota.clock()
        if not 0 <= now - value.observed_at.timestamp() <= 180:
            return frozenset()
        with self.quota.database.transaction() as db:
            old = Observation.model_validate_json(
                db.execute("SELECT observation FROM accounts WHERE key=?", (account,)).fetchone()[0]
            )
            previous = {w.name: w for w in old.windows}
            drops = {
                w.name: w
                for w in value.windows
                if w.name in previous and w.used < previous[w.name].used
            }
            row = db.execute(
                "SELECT * FROM meter_corrections WHERE account=?", (account,)
            ).fetchone()
            first, samples = now, 1
            if row:
                candidate = Observation.model_validate_json(row["observation"])
                prior = {w.name: w for w in candidate.windows}
                spacing = (value.observed_at - candidate.observed_at).total_seconds()
                if spacing < 30:
                    return frozenset()
                if (
                    spacing <= 180
                    and value.source_account == candidate.source_account
                    and value.source == candidate.source
                    and drops
                    and set(drops)
                    == {
                        name
                        for name, w in prior.items()
                        if name in previous and w.used < previous[name].used
                    }
                    and all(
                        name in prior
                        and w.resets_at == prior[name].resets_at
                        and w.used >= prior[name].used
                        for name, w in drops.items()
                    )
                ):
                    first, samples = row["first_seen"], row["samples"] + 1
            db.execute(
                "INSERT OR REPLACE INTO meter_corrections VALUES (?,?,?,?)",
                (account, value.model_dump_json(), first, samples),
            )
            if samples >= 3 and now - first >= 120:
                return frozenset(drops)
        return frozenset()

    async def loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.settings.meter_interval_seconds)

    @staticmethod
    async def stop(task: asyncio.Task) -> None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
