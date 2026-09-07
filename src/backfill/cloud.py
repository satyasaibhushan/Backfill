"""Password-protected website and an outbound-only worker connection."""

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backfill.cloud_store import CloudStore, digest, password_hash
from backfill.quota import QuotaError
from backfill.relay import validate_command

STATIC = Path(__file__).parent / "static"


def create_cloud_app(
    database_url: str | None = None, public_url: str | None = None, setup_token: str | None = None
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    lock = threading.Lock()
    store = None
    site = (public_url or os.environ.get("BACKFILL_PUBLIC_URL", "")).rstrip("/")

    def db() -> CloudStore:
        nonlocal store
        if store is None:
            with lock:
                if store is None:
                    url = (
                        database_url
                        or os.environ.get("DATABASE_URL")
                        or os.environ.get("POSTGRES_URL")
                    )
                    if not url:
                        raise HTTPException(503, "Database is not configured")
                    store = CloudStore(url)
        return store

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != site:
                return JSONResponse({"error": "Cross-origin request rejected"}, status_code=403)
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 3_500_000:
                    return JSONResponse({"error": "Request too large"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src"
            " 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return JSONResponse({"error": error.detail}, status_code=error.status_code)

    def owner(request: Request):
        session = request.cookies.get("backfill_session", "")
        found = db().one("SELECT expires FROM sessions WHERE hash=:hash", hash=digest(session))
        if not found or found["expires"] <= time.time():
            raise HTTPException(401, "Sign in to continue")
        if request.method not in ("GET", "HEAD") and request.headers.get("origin") != site:
            raise HTTPException(403, "Same-origin request required")

    def machine(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "Machine credential required")
        found = db().one(
            "SELECT * FROM machines WHERE hash=:hash AND revoked=0", hash=digest(auth[7:])
        )
        if not found:
            raise HTTPException(401, "Machine disconnected. Pair it again from the website.")
        return found

    def session_response():
        token = secrets.token_urlsafe(32)
        with db().transaction() as connection:
            connection.execute(
                text("DELETE FROM sessions WHERE expires<:now"), {"now": time.time()}
            )
            connection.execute(
                text("INSERT INTO sessions VALUES (:hash,:expires)"),
                {"hash": digest(token), "expires": time.time() + 604800},
            )
        response = JSONResponse({"signed_in": True})
        response.set_cookie(
            "backfill_session",
            token,
            httponly=True,
            secure=site.startswith("https://"),
            samesite="strict",
            max_age=604800,
        )
        return response

    @app.get("/health")
    def health():
        db().one("SELECT 1 AS ready")
        return {"ok": True, "mode": "hosted"}

    @app.get("/")
    def index(request: Request):
        try:
            owner(request)
        except HTTPException:
            return FileResponse(STATIC / "cloud-login.html")
        html = (STATIC / "index.html").read_text()
        html = html.replace(
            '<script src="/static/app.js" defer></script>',
            '<script src="/static/cloud.js" defer></script>'
            '<script src="/static/app.js" defer></script>',
        )
        html = html.replace('<div id="content"', '<div id="cloud-machine"></div><div id="content"')
        return HTMLResponse(html)

    @app.get("/static/{name}")
    def static(name: str):
        if name not in {"styles.css", "app.js", "cloud.js", "cloud-login.js"}:
            raise HTTPException(404, "Not found")
        return FileResponse(STATIC / name)

    @app.post("/cloud/login")
    async def login(request: Request):
        if not db().throttle("login"):
            raise HTTPException(429, "Too many attempts. Try again in five minutes.")
        value = await request.json()
        password = value.get("password", "") if isinstance(value, dict) else ""
        if not isinstance(password, str) or len(password) > 512:
            raise HTTPException(401, "Incorrect password")
        found = db().one("SELECT password FROM owner WHERE id=1")
        expected = found["password"] if found else password_hash("unconfigured")
        if (
            not secrets.compare_digest(password_hash(password, expected.split(":")[0]), expected)
            or not found
        ):
            raise HTTPException(401, "Incorrect password")
        return session_response()

    @app.post("/cloud/setup")
    async def setup(request: Request):
        if not db().throttle("setup"):
            raise HTTPException(429, "Too many attempts")
        value = await request.json()
        expected = setup_token or os.environ.get("BACKFILL_SETUP_TOKEN", "")
        supplied = value.get("token", "") if isinstance(value, dict) else ""
        password = value.get("password", "") if isinstance(value, dict) else ""
        if (
            not expected
            or not isinstance(supplied, str)
            or not secrets.compare_digest(supplied, expected)
        ):
            raise HTTPException(403, "Invalid setup link")
        if not isinstance(password, str) or not 12 <= len(password) <= 512:
            raise HTTPException(422, "Use at least 12 characters")
        try:
            with db().transaction() as connection:
                connection.execute(
                    text("INSERT INTO owner VALUES (1,:password)"),
                    {"password": password_hash(password)},
                )
        except IntegrityError:
            raise HTTPException(409, "Already configured. Sign in instead.") from None
        return session_response()

    @app.post("/cloud/logout", dependencies=[Depends(owner)])
    def logout(request: Request):
        with db().transaction() as connection:
            connection.execute(
                text("DELETE FROM sessions WHERE hash=:hash"),
                {"hash": digest(request.cookies.get("backfill_session", ""))},
            )
        response = JSONResponse({"signed_out": True})
        response.delete_cookie("backfill_session")
        return response

    @app.get("/cloud/machine", dependencies=[Depends(owner)])
    def machine_status():
        row = db().one("SELECT * FROM machines WHERE slot=1 AND revoked=0")
        pending = []
        if row:
            pending = db().rows(
                (
                    "SELECT id,payload,status,response FROM commands WHERE machine=:machine AND "
                    "(response IS NULL OR status>=400) ORDER BY created DESC LIMIT 20"
                ),
                machine=row["id"],
            )
        return {
            "machine": {
                "id": row["id"],
                "name": row["name"],
                "seen": row["seen"],
                "online": time.time() - row["seen"] < 45,
                "engine_error": json.loads(row["snapshot"]).get("error"),
            }
            if row
            else None,
            "changes": [
                {
                    "id": c["id"],
                    "status": c["status"],
                    "error": json.loads(c["response"]).get("error") if c["response"] else None,
                }
                for c in pending
            ],
        }

    @app.post("/cloud/pairings", dependencies=[Depends(owner)])
    def create_pairing():
        if db().one("SELECT id FROM machines WHERE slot=1 AND revoked=0"):
            raise HTTPException(409, "Disconnect the current machine before connecting another")
        token = secrets.token_urlsafe(32)
        with db().transaction() as connection:
            connection.execute(text("DELETE FROM pairings WHERE machine IS NULL"))
            connection.execute(
                text("INSERT INTO pairings VALUES (:hash,:expires,NULL)"),
                {"hash": digest(token), "expires": time.time() + 600},
            )
        return {
            "command": f"curl -fsSL '{site}/install.sh' | sh -s -- '{site}' '{token}'",
            "expires_in": 600,
        }

    @app.post("/cloud/pair")
    async def pair(request: Request):
        if not db().throttle("pair"):
            raise HTTPException(429, "Too many attempts")
        value = await request.json()
        code, credential, name = (value.get(k, "") for k in ("code", "credential", "name"))
        if (
            not all(isinstance(v, str) for v in (code, credential, name))
            or not 32 <= len(credential) <= 200
            or len(code) > 200
        ):
            raise HTTPException(422, "Invalid pairing request")
        try:
            with db().transaction() as connection:
                pairing = (
                    connection.execute(
                        text("SELECT * FROM pairings WHERE hash=:hash"), {"hash": digest(code)}
                    )
                    .mappings()
                    .first()
                )
                if not pairing:
                    raise HTTPException(401, "Pairing code expired or already used")
                if pairing["machine"]:
                    existing = connection.execute(
                        text("SELECT id FROM machines WHERE id=:id AND hash=:hash AND revoked=0"),
                        {"id": pairing["machine"], "hash": digest(credential)},
                    ).first()
                    if existing:
                        return {"id": existing[0]}
                    raise HTTPException(401, "Pairing code already used")
                key = secrets.token_hex(12)
                result = connection.execute(
                    text(
                        "UPDATE pairings SET machine=:id WHERE hash=:hash AND machine IS NULL"
                        " AND expires>:now"
                    ),
                    {"id": key, "hash": digest(code), "now": time.time()},
                )
                if result.rowcount != 1:
                    raise HTTPException(401, "Pairing code expired or already used")
                connection.execute(
                    text(
                        "INSERT INTO machines(id,slot,name,hash,seen,snapshot) VALUES "
                        "(:id,1,:name,:hash,0,'{}')"
                    ),
                    {"id": key, "name": name[:100] or "Worker", "hash": digest(credential)},
                )
        except IntegrityError:
            raise HTTPException(409, "A machine is already connected") from None
        return {"id": key}

    @app.delete("/cloud/machine/{key}", dependencies=[Depends(owner)])
    def revoke(key: str):
        with db().transaction() as connection:
            connection.execute(
                text("UPDATE machines SET revoked=1,slot=NULL WHERE id=:id"), {"id": key}
            )
        return {"disconnected": True}

    @app.post("/cloud/sync")
    async def sync(request: Request, worker: Annotated[dict, Depends(machine)]):
        value = await request.json()
        snapshot = value.get("snapshot")
        responses = value.get("responses", [])
        if (
            not isinstance(snapshot, dict)
            or not isinstance(responses, list)
            or len(responses) > 100
        ):
            raise HTTPException(422, "Invalid worker snapshot")
        with db().transaction() as connection:
            updated = connection.execute(
                text("UPDATE machines SET seen=:now,snapshot=:snapshot WHERE id=:id AND revoked=0"),
                {"now": time.time(), "snapshot": json.dumps(snapshot), "id": worker["id"]},
            )
            if updated.rowcount != 1:
                raise HTTPException(401, "Machine disconnected")
            for response in responses:
                if (
                    not isinstance(response, dict)
                    or not isinstance(response.get("body"), dict)
                    or not isinstance(response.get("status"), int)
                ):
                    raise HTTPException(422, "Invalid command response")
                connection.execute(
                    text(
                        "UPDATE commands SET response=:response,status=:status WHERE id=:id "
                        "AND machine=:machine AND response IS NULL"
                    ),
                    {
                        "response": json.dumps(response["body"]),
                        "status": response["status"],
                        "id": response.get("id"),
                        "machine": worker["id"],
                    },
                )
            commands = (
                connection.execute(
                    text(
                        "SELECT id,payload FROM commands WHERE machine=:machine AND response "
                        "IS NULL ORDER BY created,id LIMIT 10"
                    ),
                    {"machine": worker["id"]},
                )
                .mappings()
                .all()
            )
        return {
            "commands": [{"id": c["id"], "payload": json.loads(c["payload"])} for c in commands],
            "interval": 10,
            "app_connections": db().rows(
                (
                    "SELECT id,project,hash,revoked FROM app_connections WHERE "
                    "machine=:machine AND hash IS NOT NULL"
                ),
                machine=worker["id"],
            ),
        }

    def snapshot():
        row = db().one("SELECT * FROM machines WHERE slot=1 AND revoked=0")
        return (json.loads(row["snapshot"]) if row else {}), row

    @app.get("/v2/overview", dependencies=[Depends(owner)])
    def overview():
        value, worker = snapshot()
        overview = value.get("overview") or {
            "tasks": [],
            "projects": [],
            "accounts": [],
            "preferences": {"reserve": 30, "paused": False, "paused_until": None},
            "execution_enabled": False,
        }
        online = bool(worker and time.time() - worker["seen"] < 45 and not value.get("error"))
        overview["host"] = worker["name"] if worker else "No machine connected"
        if not online:
            for account in overview["accounts"]:
                account["connected"] = False
                account["execution_ready"] = False
                account["status"] = {"code": "machine_offline", "label": "Machine offline"}
        return overview

    @app.get("/v2/tasks/{key}", dependencies=[Depends(owner)])
    def task_detail(key: str):
        value, _ = snapshot()
        task = value.get("details", {}).get(key)
        if not task:
            raise HTTPException(404, "Task details have not synced yet")
        return task

    @app.get("/v2/tasks/{key}/result", dependencies=[Depends(owner)])
    def result(key: str):
        return PlainTextResponse(
            task_detail(key).get("output", ""),
            headers={"Content-Disposition": 'attachment; filename="result.md"'},
        )

    @app.api_route("/v2/{path:path}", methods=["POST", "PUT"], dependencies=[Depends(owner)])
    async def submit(path: str, request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(422, "Invalid change")
        try:
            validate_command(request.method, "/v2/" + path, body)
        except (QuotaError, ValidationError):
            raise HTTPException(422, "Check the task fields") from None
        _, worker = snapshot()
        if not worker:
            raise HTTPException(409, "Connect a machine first")
        key = request.headers.get("idempotency-key", "")
        if not 16 <= len(key) <= 100:
            raise HTTPException(422, "Request ID required")
        try:
            command = db().queue(
                worker["id"], key, {"method": request.method, "path": "/v2/" + path, "body": body}
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        if command["response"]:
            return JSONResponse(json.loads(command["response"]), status_code=command["status"])
        return JSONResponse({"queued": True, "command_id": key}, status_code=202)

    @app.get("/cloud/commands/{key}", dependencies=[Depends(owner)])
    def command_status(key: str):
        command = db().one("SELECT response,status FROM commands WHERE id=:id", id=key)
        if not command:
            raise HTTPException(404, "Change not found")
        return {
            "pending": command["response"] is None,
            "status": command["status"],
            "body": json.loads(command["response"]) if command["response"] else None,
        }

    @app.post("/v1/meters/refresh", dependencies=[Depends(owner)])
    def refresh():
        return {
            "refreshed": False,
            "message": "The worker refreshes provider readings automatically.",
        }

    @app.get("/install.sh")
    def installer():
        return FileResponse(
            Path(__file__).parent / "install-worker.sh", media_type="text/x-shellscript"
        )

    @app.get("/worker.whl")
    def wheel():
        path = Path.cwd() / "src/backfill/worker.whl"
        if not path.exists():
            raise HTTPException(503, "Worker package not available")
        return FileResponse(path, media_type="application/octet-stream")

    from backfill.cloud_apps import install

    install(app, db, owner, site)
    return app
