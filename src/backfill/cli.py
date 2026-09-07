import argparse
import asyncio
import fcntl
import json
import os
import socket
import sqlite3
import stat
import sys
import webbrowser
from pathlib import Path

import httpx
import uvicorn
from pydantic import TypeAdapter

from backfill import __version__
from backfill.auth import owner_token, private_directory, read_secret, write_secret
from backfill.config import load_settings
from backfill.main import create_app
from backfill.providers.observe import probe
from backfill.quota import QuotaError
from backfill.schemas import Key


def emit(value: object) -> None:
    print(json.dumps(value, separators=(",", ":"), allow_nan=False))


def read_json(path: str) -> dict:
    value = json.loads(sys.stdin.read() if path == "-" else Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        prog="backfill", description="Allocate quota to cooperating workloads"
    )
    cli.add_argument("--version", action="version", version=__version__)
    cli.add_argument("--data-dir", type=Path)
    cli.add_argument(
        "--credential", type=Path, help="worker token file; defaults to local owner token"
    )
    commands = cli.add_subparsers(dest="command", required=True)
    connection = commands.add_parser("connect", help="pair this machine with the website")
    connection.add_argument("--server", required=True)
    connection.add_argument("--code", required=True)
    connection.add_argument("--install", action="store_true")
    commands.add_parser("worker", help="maintain the outbound website connection")
    commands.add_parser("init", help="create private owner credential and data directory")
    commands.add_parser("serve", help="serve quota API on a private Unix socket")
    dashboard = commands.add_parser("dashboard", help="open the authenticated local dashboard")
    dashboard.add_argument("--no-open", action="store_true")
    guarded = commands.add_parser("guard", help="enforce budgets around a native executor")
    guarded.add_argument("provider", choices=("codex", "claude"))
    guarded.add_argument("key")
    guarded.add_argument("arguments", nargs=argparse.REMAINDER)
    task = commands.add_parser("task", help="submit actual work to the task queue")
    task.add_argument("--json", default="-", help="task JSON or stdin")
    commands.add_parser("demo", help="run deterministic, isolated quota scenarios as JSONL")
    status = commands.add_parser("status", help="account overview or a workload's quota and grants")
    status.add_argument("key", nargs="?")
    status.add_argument("--grant", dest="grant_id", help="inspect one grant by ID")
    for name in ("account", "workload", "observe", "acquire", "budget", "project"):
        command = commands.add_parser(name)
        command.add_argument("key")
        command.add_argument("--json", default="-", help="JSON file or - for stdin")
    commands.choices["workload"].add_argument("--token-out", type=Path)
    rotate = commands.add_parser("rotate-token")
    rotate.add_argument("key")
    rotate.add_argument("--token-out", type=Path, required=True)
    for name in ("report", "release"):
        command = commands.add_parser(name)
        command.add_argument("key")
        command.add_argument("grant_id")
    commands.choices["report"].add_argument("--json", default="-")
    reader = commands.add_parser("probe", help="read provider quota without making a model request")
    reader.add_argument("provider", choices=("codex", "claude"))
    reader.add_argument("--account", help="submit observation to this configured account")
    return cli


def run(args: argparse.Namespace) -> int:
    for field in ("key", "account", "grant_id"):
        value = getattr(args, field, None)
        if value is not None:
            TypeAdapter(Key).validate_python(value)
    settings = load_settings()
    if args.data_dir:
        settings.data_dir = args.data_dir
    if args.command == "connect":
        from backfill.worker import connect

        emit(connect(settings.root, args.server, args.code, args.install))
        return 0
    if args.command == "worker":
        from backfill.worker import Worker

        with (settings.root / "connection.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            asyncio.run(Worker(settings.root).run())
        return 0
    if args.command == "demo":
        from backfill.demo import run_demo

        for event in run_demo():
            emit(event)
        return 0
    if args.command == "init":
        owner_token(settings.root)
        emit(
            {
                "data_dir": str(settings.root),
                "socket": str(settings.socket),
                "owner_credential": str(settings.root / "owner.token"),
            }
        )
        return 0
    if args.command == "serve":
        owner_token(settings.root)
        os.umask(0o077)
        with (settings.root / "service.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise QuotaError("quota service already running") from error
            if settings.socket.exists():
                if not stat.S_ISSOCK(settings.socket.lstat().st_mode):
                    raise QuotaError("socket path is occupied by a non-socket")
                settings.socket.unlink()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(settings.socket))
                settings.socket.chmod(0o600)
                config = uvicorn.Config(
                    create_app(settings), proxy_headers=False, access_log=False, log_level="warning"
                )
                try:
                    if settings.dashboard_port:
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as web:
                            web.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                            web.bind(("127.0.0.1", settings.dashboard_port))
                            uvicorn.Server(config).run(sockets=[listener, web])
                    else:
                        uvicorn.Server(config).run(sockets=[listener])
                finally:
                    settings.socket.unlink(missing_ok=True)
        return 0
    observation = None
    if args.command == "probe":
        observation = asyncio.run(probe(args.provider, settings)).model_dump(mode="json")
        if not args.account:
            emit(observation)
            return 0
    token = read_secret(args.credential or settings.root / "owner.token")
    if args.command == "guard":
        from backfill.guard import guard

        return asyncio.run(guard(args.key, args.provider, args.arguments, settings, token))
    transport = httpx.HTTPTransport(uds=str(settings.socket))
    with httpx.Client(
        transport=transport,
        base_url="http://backfill",
        timeout=45,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        if args.command == "dashboard":
            response = client.post("/v1/dashboard/ticket")
            if response.is_error:
                raise QuotaError("could not create dashboard session")
            result = response.json()
            if not result["port"]:
                raise QuotaError("dashboard listener is disabled")
            url = f"http://127.0.0.1:{result['port']}/?connect={os.urandom(4).hex()}#ticket={result['ticket']}"
            if args.no_open:
                emit({"url": url, "expires_in_seconds": 60})
            else:
                webbrowser.open(url)
                emit({"opened": True, "url": f"http://127.0.0.1:{result['port']}/"})
            return 0
        elif args.command == "task":
            response = client.post("/v2/tasks", json=read_json(args.json))
        elif args.command == "status":
            if args.grant_id and not args.key:
                raise ValueError("a grant requires a workload key")
            path = f"/v1/workloads/{args.key}" if args.key else "/v1/status"
            if args.grant_id:
                path += f"/grants/{args.grant_id}"
            response = client.get(path)
        elif args.command == "probe":
            response = client.post(f"/v1/accounts/{args.account}/observations", json=observation)
        elif args.command == "account":
            response = client.put(f"/v1/accounts/{args.key}", json=read_json(args.json))
        elif args.command == "project":
            response = client.put(f"/v1/projects/{args.key}", json=read_json(args.json))
        elif args.command == "budget":
            response = client.put(f"/v1/workloads/{args.key}/budget", json=read_json(args.json))
        elif args.command == "workload":
            response = client.put(f"/v1/workloads/{args.key}", json=read_json(args.json))
        elif args.command == "observe":
            response = client.post(
                f"/v1/accounts/{args.key}/observations", json=read_json(args.json)
            )
        elif args.command == "acquire":
            response = client.post(f"/v1/workloads/{args.key}/acquire", json=read_json(args.json))
        elif args.command == "rotate-token":
            if args.token_out.exists() or args.token_out.is_symlink():
                raise ValueError("token output already exists; choose a new private path")
            response = client.post(f"/v1/workloads/{args.key}/rotate-token")
        else:
            path = f"/v1/workloads/{args.key}/grants/{args.grant_id}/{args.command}"
            response = client.post(
                path, json=read_json(args.json) if args.command == "report" else None
            )
        result = response.json()
        if response.is_error:
            # Never echo request bodies or validation values into logs.
            emit({"error": result.get("error", "request rejected"), "status": response.status_code})
            return 1
        if args.command in ("workload", "rotate-token"):
            new_token = result.pop("token", None)
            if new_token:
                credentials = settings.root / "credentials"
                private_directory(credentials)
                target = args.token_out or credentials / f"{args.key}.token"
                try:
                    write_secret(target, new_token)
                except OSError as error:
                    raise QuotaError(
                        "workload saved but credential write failed; rotate its token"
                    ) from error
                result["credential_file"] = str(target.absolute())
        emit(result)
        return 2 if result.get("decision") == "wait" else 0


def main() -> None:
    try:
        code = run(parser().parse_args())
    except (QuotaError, OSError, ValueError, RuntimeError, sqlite3.Error, httpx.HTTPError) as error:
        message = (
            str(error) if isinstance(error, QuotaError | RuntimeError) else "local operation failed"
        )
        emit({"error": message, "type": type(error).__name__})
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
