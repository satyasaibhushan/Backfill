"""Small process client for applications using project-scoped quota admission."""

import asyncio
import json
import os
import secrets
import signal
import sys
import tempfile
from pathlib import Path

import httpx

from backfill.auth import private_directory, read_secret, write_secret
from backfill.worker import check_server


def connect_app(settings, server, code):
    server = check_server(server)
    directory = Path("~/.local/share/backfill/connections").expanduser()
    private_directory(directory)
    # Keep the credential on disk before redemption, so lost responses can be retried.
    import hashlib

    pending = directory / (hashlib.sha256(code.encode()).hexdigest() + ".pending")
    if not pending.exists():
        write_secret(pending, secrets.token_urlsafe(48))
    credential = read_secret(pending)
    response = httpx.post(
        server + "/cloud/apps/redeem", json={"code": code, "credential": credential}, timeout=30
    )
    response.raise_for_status()
    value = response.json()
    root = settings.root
    worker_root = Path("~/.local/share/backfill-worker/data").expanduser()
    if not settings.socket.exists() and (worker_root / "quota.sock").exists():
        root = worker_root
    connection = directory / (value["id"] + ".json")
    if not connection.exists():
        write_secret(
            connection,
            json.dumps({**value, "server": server, "credential": credential, "root": str(root)}),
        )
    pending.unlink(missing_ok=True)
    return {"connected": True, "project": value["project"], "connection": str(connection)}


def local_root(settings):
    worker = Path("~/.local/share/backfill-worker/data").expanduser()
    return settings.root if settings.socket.exists() else worker


def local_projects(settings):
    root = local_root(settings)
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=str(root / "quota.sock")),
        base_url="http://backfill",
        timeout=15,
        headers={"Authorization": "Bearer " + read_secret(root / "owner.token")},
    ) as client:
        response = client.get("/v2/app-projects")
        response.raise_for_status()
        return response.json()


def connect_local(settings, project, key):
    import hashlib

    root = local_root(settings)
    directory = Path("~/.local/share/backfill/connections").expanduser()
    private_directory(directory)
    ident = "local-" + hashlib.sha256((key + ":" + project).encode()).hexdigest()[:32]
    path = directory / (ident + ".json")
    pending = directory / (ident + ".pending")
    if path.exists():
        credential = json.loads(read_secret(path))["credential"]
    else:
        if not pending.exists():
            write_secret(pending, secrets.token_urlsafe(48))
        credential = read_secret(pending)
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=str(root / "quota.sock")),
        base_url="http://backfill",
        timeout=15,
        headers={"Authorization": "Bearer " + read_secret(root / "owner.token")},
    ) as client:
        response = client.post(
            "/v2/app-connections", json={"project": project, "key": key, "credential": credential}
        )
        response.raise_for_status()
        value = response.json()
    write_secret(path, json.dumps({**value, "credential": credential, "root": str(root)}))
    pending.unlink(missing_ok=True)
    return {"connected": True, "project": project, "connection": str(path)}


def emit(value):
    print(json.dumps(value), flush=True)


async def execute(connection_path, request):
    config = json.loads(read_secret(connection_path))
    root = Path(config["root"])
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=str(root / "quota.sock")),
        base_url="http://backfill",
        timeout=15,
        headers={"Authorization": "Bearer " + config["credential"]},
    ) as client:

        async def call(path, value=None):
            response = await (client.get(path) if value is None else client.post(path, json=value))
            response.raise_for_status()
            return response.json()

        job = await call("/v2/external/tasks", request)
        base = "/v2/external/tasks/" + job["id"]
        if job["state"] in ("review", "done", "failed", "cancelled", "paused"):
            emit(
                {
                    "type": "result",
                    "state": job["state"],
                    "provider": job.get("selected_provider"),
                    "output": job["output"],
                    "task_id": job["id"],
                    "reason": job.get("reason", ""),
                }
            )
            return 0
        permit = await call(base + "/start", {})
        if permit["decision"] != "granted":
            emit(
                {
                    "type": "result",
                    "state": "waiting",
                    "reason": permit["reason"],
                    "task_id": job["id"],
                    "output": job.get("output", ""),
                }
            )
            return 0
        provider = permit["provider"]
        cwd = (
            str(Path(job["folder"]).expanduser())
            if job["folder"]
            else str(
                Path(request.get("workspace") or "~/.local/share/backfill/work").expanduser()
                / job["id"]
            )
        )
        Path(cwd).mkdir(parents=True, exist_ok=True)
        prompt = job["instructions"]
        if job.get("output"):
            prompt += "\n\nSaved progress, continue from here:\n" + job["output"][-30000:]
        output, reason, state = "", "", "failed"
        completed, denied = False, False
        summary = {}
        process = None
        with tempfile.TemporaryDirectory(prefix="backfill-run-") as temporary:
            token_path = Path(temporary) / "token"
            write_secret(token_path, permit["credential"])
            command = [
                sys.executable,
                "-m",
                "backfill.cli",
                "--data-dir",
                str(root),
                "--credential",
                str(token_path),
                "guard",
                provider,
                permit["workload"],
            ]
            if provider == "claude":
                command += [
                    "--tools",
                    "Read,Glob,Grep,WebFetch,WebSearch"
                    + (",Write,Edit,Bash" if job["access"] == "edit" else ""),
                    "--permission-mode",
                    "acceptEdits" if job["access"] == "edit" else "dontAsk",
                ]
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=8 * 1024 * 1024,
            )
            loop = asyncio.get_running_loop()

            def stop():
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

            loop.add_signal_handler(signal.SIGTERM, stop)

            async def send(value):
                process.stdin.write((json.dumps(value) + "\n").encode())
                await process.stdin.drain()

            async def errors():
                nonlocal summary
                while line := await process.stderr.readline():
                    try:
                        summary = json.loads(line)
                    except ValueError:
                        pass

            async def progress():
                while process.returncode is None:
                    await asyncio.sleep(3)
                    await call(base + "/progress", {"output": output})
                    emit(
                        {
                            "type": "progress",
                            "output": output,
                            "provider": provider,
                            "task_id": job["id"],
                        }
                    )

            async def read():
                nonlocal output, completed, denied
                while line := await process.stdout.readline():
                    event = json.loads(line)
                    if provider == "claude":
                        if event.get("type") == "assistant":
                            output += "\n\n" + "\n".join(
                                p.get("text", "")
                                for p in event.get("message", {}).get("content", [])
                                if p.get("type") == "text"
                            )
                        if event.get("type") == "result":
                            output = event.get("result") or output
                            completed = not event.get("is_error")
                            denied = bool(event.get("permission_denials"))
                    elif event.get("id") == 1 and "result" in event:
                        await send({"method": "initialized", "params": {}})
                        await send(
                            {
                                "id": 2,
                                "method": "thread/start",
                                "params": {
                                    "cwd": cwd,
                                    "approvalPolicy": "never",
                                    "sandbox": "workspace-write"
                                    if job["access"] == "edit"
                                    else "read-only",
                                },
                            }
                        )
                    elif event.get("id") == 2 and "result" in event:
                        await send(
                            {
                                "id": 3,
                                "method": "turn/start",
                                "params": {
                                    "threadId": event["result"]["thread"]["id"],
                                    "input": [{"type": "text", "text": prompt}],
                                },
                            }
                        )
                    elif event.get("method") == "item/completed":
                        item = event.get("params", {}).get("item", {})
                        if item.get("type") == "agentMessage":
                            output = item.get("text", "")
                    elif event.get("method") == "turn/completed":
                        completed = (
                            event.get("params", {}).get("turn", {}).get("status") == "completed"
                        )
                        process.stdin.close()
                    elif "id" in event and event.get("method"):
                        denied = True
                        await send(
                            {
                                "id": event["id"],
                                "error": {"code": -32000, "message": "Human approval required"},
                            }
                        )
                    elif event.get("error"):
                        process.stdin.close()

            if provider == "claude":
                process.stdin.write(prompt.encode())
                await process.stdin.drain()
                process.stdin.close()
            else:
                await send(
                    {
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {"name": "backfill", "version": "0.3.0"},
                            "capabilities": {"experimentalApi": True},
                        },
                    }
                )
            jobs = [
                asyncio.create_task(read()),
                asyncio.create_task(errors()),
                asyncio.create_task(progress()),
            ]
            waiter = asyncio.create_task(process.wait())
            try:
                done, _ = await asyncio.wait([waiter, *jobs], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if not waiter.done():
                    done, _ = await asyncio.wait(
                        [waiter, jobs[2]], return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
                await waiter
                await asyncio.gather(*jobs[:2])
                state = (
                    "review"
                    if completed and process.returncode == 0
                    else "waiting"
                    if summary.get("decision") == "wait"
                    or summary.get("reason") in ("budget", "interrupted")
                    else "failed"
                )
                reason = "Needs human approval" if denied else str(summary.get("reason", ""))
            except BaseException:
                stop()
                raise
            finally:
                stop()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
                for task in jobs + [waiter]:
                    task.cancel()
                await asyncio.gather(*jobs, waiter, return_exceptions=True)
                loop.remove_signal_handler(signal.SIGTERM)
        for retry in range(5):
            try:
                await call(base + "/progress", {"state": state, "output": output, "reason": reason})
                break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409 or retry == 4:
                    raise
                await asyncio.sleep(1)
        emit(
            {
                "type": "result",
                "state": state,
                "reason": reason,
                "output": output,
                "provider": provider,
                "task_id": job["id"],
            }
        )
        return 0
