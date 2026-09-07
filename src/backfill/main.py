import asyncio
import json
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backfill.auth import owner_token
from backfill.config import Settings, load_settings
from backfill.database import Database
from backfill.execution import Execution
from backfill.governor import Governor
from backfill.meter import Meter
from backfill.quota import QuotaError, QuotaService
from backfill.schemas import (
    AccountInput,
    Acquire,
    Key,
    Observation,
    PauseUntil,
    Policy,
    ProjectBudget,
    Report,
    RunStart,
    RunUsage,
    TaskBudget,
    WorkloadInput,
)
from backfill.tasks import Preferences, ProjectInput, TaskAction, TaskInput, Tasks


def create_app(settings: Settings | None = None, service: QuotaService | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.owner = owner_token(settings.root)
        app.state.quota = service or QuotaService(Database(settings.root / "quota.db"))
        app.state.governor = Governor(app.state.quota)
        app.state.meter = Meter(app.state.quota, settings)
        app.state.tasks = Tasks(app.state.quota, settings)
        app.state.execution = Execution(app.state.tasks, app.state.governor, app.state.meter)
        app.state.tickets = {}
        app.state.sessions = {}
        task = None
        if settings.meter_enabled:
            app.state.meter.defaults()
            task = asyncio.create_task(app.state.meter.loop())
        runner = None
        if settings.automation_enabled:
            app.state.tasks.recover()
            app.state.tasks.set_preferences(app.state.tasks.preferences())
            runner = asyncio.create_task(app.state.execution.loop())
        try:
            yield
        finally:
            if runner:
                runner.cancel()
                await app.state.execution.close()
                await asyncio.gather(runner, return_exceptions=True)
            if task:
                await Meter.stop(task)

    app = FastAPI(
        title="Backfill",
        version="0.3.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "backfill", "testserver"],
    )

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and origin
            and origin != str(request.base_url).rstrip("/")
        ):
            return JSONResponse({"error": "cross-origin writes are not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(QuotaError)
    async def quota_error(request: Request, error: QuotaError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content={"error": error.message})

    def quota(request: Request) -> QuotaService:
        return request.app.state.quota

    def identity(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
        session = request.cookies.get("backfill_session")
        if (
            not authorization
            and session
            and request.app.state.sessions.get(session, 0) > time.time()
        ):
            if request.method not in ("GET", "HEAD") and request.headers.get("origin") != str(
                request.base_url
            ).rstrip("/"):
                raise QuotaError("same-origin request required", 403)
            return "@owner"
        if not authorization or not authorization.startswith("Bearer "):
            raise QuotaError("bearer token required", 401)
        token = authorization.removeprefix("Bearer ")
        if secrets.compare_digest(token, request.app.state.owner):
            return "@owner"
        key = request.app.state.quota.authenticate_worker(token)
        if key is None:
            raise QuotaError("invalid bearer token", 401)
        return key

    def owner(principal: Annotated[str, Depends(identity)]) -> None:
        if principal != "@owner":
            raise QuotaError("owner credential required", 403)

    def worker(key: Key, principal: Annotated[str, Depends(identity)]) -> None:
        if principal not in ("@owner", key):
            raise QuotaError("credential cannot access this workload", 403)

    Quota = Annotated[QuotaService, Depends(quota)]
    owned = [Depends(owner)]
    scoped = [Depends(worker)]

    @app.get("/v1/status", dependencies=owned)
    def overview(service: Quota, request: Request) -> dict:
        return {**service.overview(), **request.app.state.governor.overview()}

    @app.put("/v1/accounts/{key}", dependencies=owned)
    def account(key: Key, value: AccountInput, service: Quota, request: Request) -> dict:
        if value.provider:
            request.app.state.meter.bind(key, value.provider, value.policy)
            return {"account": key, "policy": value.policy.model_dump(mode="json")}
        return service.set_account(key, value.policy)

    @app.post("/v1/accounts/{key}/observations", dependencies=owned)
    def observe(key: Key, value: Observation, service: Quota) -> dict:
        return service.observe(key, value)

    @app.put("/v1/workloads/{key}", dependencies=owned)
    def workload(key: Key, value: WorkloadInput, service: Quota) -> dict:
        return service.set_workload(key, value)

    @app.post("/v1/workloads/{key}/rotate-token", dependencies=owned)
    def rotate(key: Key, service: Quota) -> dict:
        return service.rotate_token(key)

    @app.get("/v1/workloads/{key}", dependencies=scoped)
    def status(key: Key, service: Quota) -> dict:
        return service.status(key)

    @app.post("/v1/workloads/{key}/acquire", dependencies=scoped)
    def acquire(key: Key, value: Acquire, service: Quota) -> dict:
        return service.acquire(key, value).model_dump(mode="json")

    @app.get("/v1/workloads/{key}/grants/{grant_id}", dependencies=scoped)
    def grant_status(key: Key, grant_id: Key, service: Quota) -> dict:
        return service.grant_status(key, grant_id)

    @app.post("/v1/workloads/{key}/grants/{grant_id}/report", dependencies=scoped)
    def report(key: Key, grant_id: str, value: Report, service: Quota) -> dict:
        return service.report(key, grant_id, value)

    @app.post("/v1/workloads/{key}/grants/{grant_id}/release", dependencies=scoped)
    def release(key: Key, grant_id: str, service: Quota) -> dict:
        return service.release(key, grant_id)

    @app.put("/v1/projects/{key}", dependencies=owned)
    def project(key: Key, value: ProjectBudget, request: Request) -> dict:
        return request.app.state.governor.set_project(key, value)

    @app.put("/v1/pause", dependencies=owned)
    def pause_all(value: PauseUntil, service: Quota) -> dict:
        with service.database.transaction() as db:
            for row in db.execute("SELECT key,policy FROM accounts").fetchall():
                policy = Policy.model_validate_json(row["policy"])
                policy.paused = False
                policy.paused_until = value.paused_until
                db.execute(
                    "UPDATE accounts SET policy=? WHERE key=?",
                    (policy.model_dump_json(), row["key"]),
                )
        return {"paused_until": value.paused_until}

    @app.put("/v1/workloads/{key}/budget", dependencies=owned)
    def budget(key: Key, value: TaskBudget, request: Request) -> dict:
        return request.app.state.governor.set_budget(key, value)

    @app.post("/v1/workloads/{key}/runs", dependencies=scoped)
    def start_run(key: Key, value: RunStart, request: Request) -> dict:
        return request.app.state.governor.start(key, value)

    @app.get("/v1/workloads/{key}/runs/{run_id}", dependencies=scoped)
    def run_status(key: Key, run_id: Key, request: Request) -> dict:
        return request.app.state.governor.status(key, run_id)

    @app.post("/v1/workloads/{key}/runs/{run_id}/usage", dependencies=scoped)
    def run_usage(key: Key, run_id: Key, value: RunUsage, request: Request) -> dict:
        return request.app.state.governor.report(key, run_id, value)

    @app.post("/v1/meters/refresh", dependencies=owned)
    async def refresh(request: Request) -> dict:
        await request.app.state.meter.refresh()
        return {"refreshed": True}

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": "0.3.0"}

    @app.get("/v2/overview", dependencies=owned)
    def task_overview(request: Request) -> dict:
        import socket

        result = request.app.state.tasks.list()
        for item in result["tasks"]:
            item.pop("output", None)
            item.pop("feedback", None)
            item.pop("instructions", None)
        current = request.app.state.quota.overview()
        with request.app.state.quota.database.transaction() as db:
            meters = {r["account"]: dict(r) for r in db.execute("SELECT * FROM meters")}
            readings = {r["account"]: dict(r) for r in db.execute("SELECT * FROM meter_readings")}
        accounts = []
        for account in current["accounts"]:
            meter = meters.get(account["key"])
            if not meter:
                continue
            reading = readings.get(account["key"])
            obs = (
                json.loads(reading["observation"])
                if reading and reading["observation"]
                else account["observation"]
            )
            identity_matches = not account["observation"] or (
                obs and obs["source_account"] == account["observation"]["source_account"]
            )
            windows = obs["windows"] if obs else []
            groups = []
            for label, predicate in [
                ("Session", lambda w: w["duration_seconds"] <= 86400),
                ("Weekly", lambda w: w["duration_seconds"] > 86400),
                ("Fable weekly", lambda w: w["name"] == "extra.claude-weekly-scoped-fable"),
            ]:
                group = [
                    w
                    for w in windows
                    if predicate(w)
                    and (label == "Fable weekly" or w["name"] != "extra.claude-weekly-scoped-fable")
                ]
                if label == "Fable weekly" and not group and meter["provider"] == "claude":
                    groups.append({"label": label, "remaining": None, "resets_at": None})
                if group:
                    tightest = min(group, key=lambda w: (w["limit"] - w["used"]) / w["limit"])
                    groups.append(
                        {
                            "label": label,
                            "remaining": round(
                                max(
                                    0,
                                    100
                                    * (tightest["limit"] - tightest["used"])
                                    / tightest["limit"],
                                ),
                                1,
                            ),
                            "resets_at": tightest["resets_at"],
                        }
                    )
            from datetime import datetime

            stale = (
                not obs
                or time.time() - datetime.fromisoformat(obs["observed_at"]).timestamp()
                > account["policy"]["snapshot_ttl_seconds"]
            )
            accounts.append(
                {
                    "provider": meter["provider"],
                    "connected": bool(
                        not stale
                        and identity_matches
                        and not (reading["error"] if reading else meter["error"])
                    ),
                    "execution_ready": not stale and not meter["error"],
                    "windows": groups,
                    "checked_at": meter["checked_at"],
                }
            )
        return {
            **result,
            "accounts": accounts,
            "host": socket.gethostname(),
            "execution_enabled": settings.automation_enabled,
        }

    @app.post("/v2/tasks", dependencies=owned, status_code=201)
    def create_task(value: TaskInput, request: Request) -> dict:
        return request.app.state.tasks.create(value)

    @app.get("/v2/tasks/{key}", dependencies=owned)
    def get_task(key: Key, request: Request) -> dict:
        return request.app.state.tasks.get(key)

    @app.put("/v2/tasks/{key}", dependencies=owned)
    def edit_task(key: Key, value: TaskInput, request: Request) -> dict:
        return request.app.state.tasks.update(key, value)

    @app.post("/v2/tasks/{key}/actions", dependencies=owned)
    def task_action(key: Key, value: TaskAction, request: Request) -> dict:
        return request.app.state.tasks.action(key, value)

    @app.get("/v2/tasks/{key}/result", dependencies=owned)
    def task_result(key: Key, request: Request):
        from fastapi.responses import PlainTextResponse

        job = request.app.state.tasks.get(key)
        return PlainTextResponse(
            job["output"], headers={"Content-Disposition": 'attachment; filename="result.md"'}
        )

    @app.post("/v2/projects", dependencies=owned, status_code=201)
    def create_project(value: ProjectInput, request: Request) -> dict:
        return request.app.state.tasks.project(value)

    @app.put("/v2/projects/{key}", dependencies=owned)
    def edit_project(key: Key, value: ProjectInput, request: Request) -> dict:
        return request.app.state.tasks.project(value, key)

    @app.put("/v2/preferences", dependencies=owned)
    def preferences(value: Preferences, request: Request) -> dict:
        return request.app.state.tasks.set_preferences(value)

    @app.post("/v1/dashboard/ticket", dependencies=owned)
    def ticket(request: Request) -> dict:
        now = time.time()
        request.app.state.tickets = {k: v for k, v in request.app.state.tickets.items() if v > now}
        value = secrets.token_urlsafe(32)
        request.app.state.tickets[value] = now + 60
        return {"ticket": value, "port": settings.dashboard_port}

    @app.post("/v1/dashboard/session")
    async def session(request: Request) -> JSONResponse:
        value = await request.json()
        if not isinstance(value, dict) or not isinstance(value.get("ticket"), str):
            raise QuotaError("a dashboard ticket is required", 401)
        if request.app.state.tickets.pop(value["ticket"], 0) <= time.time():
            raise QuotaError("dashboard link expired; open a new one", 401)
        token = secrets.token_urlsafe(32)
        now = time.time()
        request.app.state.sessions = {
            k: v for k, v in request.app.state.sessions.items() if v > now
        }
        request.app.state.sessions[token] = now + 8 * 3600
        response = JSONResponse({"connected": True})
        response.set_cookie(
            "backfill_session", token, httponly=True, samesite="strict", max_age=8 * 3600
        )
        return response

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def dashboard() -> FileResponse:
        return FileResponse(static / "index.html")

    return app


app = create_app()
