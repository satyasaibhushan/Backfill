"""Outbound hosted connection. Local command receipts survive lost responses."""

import asyncio
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from backfill.auth import private_directory, read_secret, write_secret


def check_server(server: str) -> str:
    url = urlparse(server)
    if url.scheme != "https" and not (
        url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost"}
    ):
        raise ValueError("The website must use HTTPS")
    if (
        not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        raise ValueError("Use the website origin without a path")
    return server.rstrip("/")


def connect(root: Path, server: str, code: str, install: bool = False):
    server = check_server(server)
    private_directory(root)
    pending = root / "pairing.json"
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    saved = json.loads(read_secret(pending)) if pending.exists() else {}
    if saved.get("code_hash") != code_hash:
        pending.unlink(missing_ok=True)
        saved = {"code_hash": code_hash, "credential": secrets.token_urlsafe(32)}
        write_secret(pending, json.dumps(saved))
    token = saved["credential"]
    with httpx.Client(timeout=30) as client:
        response = client.post(
            server + "/cloud/pair",
            json={"code": code, "credential": token, "name": socket.gethostname()},
        )
        if response.status_code != 200:
            raise RuntimeError(response.json().get("error", "Pairing failed"))
    credential_file = root / "machine.token"
    credential_file.unlink(missing_ok=True)
    write_secret(credential_file, token)
    config = root / "connection.json"
    # No website password or provider credential enters the connection file.
    if config.exists():
        config.unlink()
    write_secret(config, json.dumps({"server": server, "machine": response.json()["id"]}))
    if install:
        install_services(root)
    return {"paired": True, "website": server, "service_installed": install}


def install_services(root: Path):
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Persistent worker installation currently requires Linux")
    import shlex

    directory = Path.home() / ".config/systemd/user"
    directory.mkdir(parents=True, exist_ok=True)
    executable = Path(sys.executable).parent / "backfill"
    env_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    for name, command in (("backfill", "serve"), ("backfill-connection", "worker")):
        unit = f"""[Unit]
Description=Backfill {command}
After=network-online.target
[Service]
Type=simple
ExecStart={shlex.quote(str(executable))} --data-dir {shlex.quote(str(root))} {command}
Environment="PATH={env_path}"
Environment=BACKFILL_DASHBOARD_PORT=0
Environment=BACKFILL_HOSTED_WORKER=true
Restart=always
RestartSec=5
UMask=0077
[Install]
WantedBy=default.target
"""
        target = directory / f"{name}.service"
        if target.exists() and str(executable) not in target.read_text():
            raise RuntimeError("A different service already uses " + name)
        target.write_text(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        [
            "systemctl",
            "--user",
            "enable",
            "--now",
            "backfill.service",
            "backfill-connection.service",
        ],
        check=True,
    )
    linger = subprocess.run(
        ["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if linger != "yes":
        result = subprocess.run(
            ["loginctl", "enable-linger", str(os.getuid())], capture_output=True
        )
        if result.returncode:
            raise RuntimeError(
                "Paired, but persistence after logout requires: sudo loginctl enable-linger "
                + str(os.getuid())
            )


class Worker:
    def __init__(self, root: Path):
        self.root = root
        config = json.loads(read_secret(root / "connection.json"))
        self.server = check_server(config["server"])
        self.token = read_secret(root / "machine.token")
        self.responses = []
        self.snapshot = {}

    async def sync(self, local: httpx.AsyncClient, remote: httpx.AsyncClient):
        try:
            response = await local.get("/v2/overview")
            response.raise_for_status()
            overview = response.json()
            details = {}
            for task in overview["tasks"]:
                result = await local.get("/v2/tasks/" + task["id"])
                result.raise_for_status()
                details[task["id"]] = result.json()
            self.snapshot = {"overview": overview, "details": details}
        except (httpx.HTTPError, ValueError):
            self.snapshot["error"] = "Local task service unavailable"
        response = await remote.post(
            self.server + "/cloud/sync",
            json={"snapshot": self.snapshot, "responses": self.responses},
            headers={"Authorization": "Bearer " + self.token},
        )
        if response.status_code == 401:
            (self.root / "connection.seen").unlink(missing_ok=True)
            raise PermissionError("Machine disconnected. Pair it again from the website.")
        response.raise_for_status()
        value = response.json()
        self.responses = []
        grant_sync = await local.put("/v2/app-grants", json=value.get("app_connections", []))
        grant_sync.raise_for_status()
        (self.root / "connection.seen").write_text(str(time.time()))
        for command in value["commands"]:
            # The local receipt and task change are one transaction. Redelivery is safe.
            result = await local.post("/v1/relay/command", json=command)
            result.raise_for_status()
            self.responses.append({"id": command["id"], **result.json()})
        return 0.2 if self.responses else value.get("interval", 10)

    async def run(self):
        delay = 1
        async with (
            httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=str(self.root / "quota.sock")),
                base_url="http://backfill",
                headers={"Authorization": "Bearer " + read_secret(self.root / "owner.token")},
                timeout=30,
            ) as local,
            httpx.AsyncClient(timeout=30) as remote,
        ):
            while True:
                try:
                    delay = await self.sync(local, remote)
                except PermissionError:
                    print("Machine disconnected. Pair again on the website.", flush=True)
                    return
                except (httpx.HTTPError, ValueError, OSError):
                    delay = min(max(delay * 2, 2), 30)
                    print("Connection interrupted; retrying automatically.", flush=True)
                await asyncio.sleep(delay)
