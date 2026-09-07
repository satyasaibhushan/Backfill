"""Owner-issued connections scoped to one project and execution machine."""

import json
import secrets
import shlex
import time

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text

from backfill.cloud_store import digest


def install(app, store, owner, site):
    def project(key):
        machine = store().one("SELECT * FROM machines WHERE slot=1 AND revoked=0")
        projects = (
            json.loads(machine["snapshot"]).get("overview", {}).get("projects", [])
            if machine
            else []
        )
        if not machine or not any(p["id"] == key for p in projects):
            raise HTTPException(404, "Connect a machine and select an existing project")
        return machine

    @app.get("/cloud/projects/{key}/connections", dependencies=[Depends(owner)])
    def connections(key: str):
        project(key)
        return store().rows(
            (
                "SELECT id,name,revoked,hash IS NOT NULL AS connected FROM "
                "app_connections WHERE project=:project"
            ),
            project=key,
        )

    @app.post("/cloud/projects/{key}/connections", dependencies=[Depends(owner)])
    async def create(key: str, request: Request):
        machine = project(key)
        value = await request.json()
        name = str(value.get("name", "Application")).strip()[:100]
        token, ident = secrets.token_urlsafe(32), secrets.token_hex(12)
        with store().transaction() as db:
            db.execute(
                text(
                    "INSERT INTO "
                    "app_connections(id,project,machine,name,code,expires,revoked) "
                    "VALUES (:id,:project,:machine,:name,:code,:expires,0)"
                ),
                dict(
                    id=ident,
                    project=key,
                    machine=machine["id"],
                    name=name,
                    code=digest(token),
                    expires=time.time() + 600,
                ),
            )
        command = (
            '"$HOME/.local/share/backfill-worker/venv/bin/backfill" connect-app --server '
            + shlex.quote(site)
            + " --code "
            + shlex.quote(token)
        )
        return {"id": ident, "command": command, "expires_in": 600}

    @app.delete("/cloud/projects/{key}/connections/{ident}", dependencies=[Depends(owner)])
    def revoke(key: str, ident: str):
        project(key)
        with store().transaction() as db:
            db.execute(
                text("UPDATE app_connections SET revoked=1 WHERE id=:id AND project=:project"),
                {"id": ident, "project": key},
            )
        return {"revoked": True}

    @app.post("/cloud/apps/redeem")
    async def redeem(request: Request):
        if not store().throttle("application-pairing"):
            raise HTTPException(429, "Try again later")
        value = await request.json()
        code, credential = value.get("code"), value.get("credential")
        if (
            not isinstance(code, str)
            or not isinstance(credential, str)
            or not 40 <= len(credential) <= 128
        ):
            raise HTTPException(422, "Invalid pairing request")
        with store().transaction() as db:
            row = (
                db.execute(
                    text(
                        "UPDATE app_connections SET hash=:hash WHERE code=:code "
                        "AND expires>:now AND revoked=0 AND (hash IS NULL OR "
                        "hash=:hash) RETURNING id,project,machine"
                    ),
                    {"hash": digest(credential), "code": digest(code), "now": time.time()},
                )
                .mappings()
                .first()
            )
            if not row:
                raise HTTPException(401, "Connection code expired or already used")
            machine = db.execute(
                text("SELECT id FROM machines WHERE id=:id AND revoked=0"), {"id": row["machine"]}
            ).first()
            if not machine:
                raise HTTPException(401, "Execution machine was disconnected")
        return {"id": row["id"], "project": row["project"]}
