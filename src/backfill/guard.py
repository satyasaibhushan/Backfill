"""A process boundary for native executors, with no workflow or prompt ownership."""

import asyncio
import json
import os
import secrets
import signal
import stat
import sys
import time
from contextlib import suppress

import httpx

from backfill.config import Settings
from backfill.providers.process import stop_probe
from backfill.quota import QuotaError
from backfill.usage import Usage, UsageError


async def guard(
    key: str, provider: str, arguments: list[str], settings: Settings, token: str
) -> int:
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    overrides = (
        (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        )
        if provider == "claude"
        else ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL")
    )
    if any(os.environ.get(name) for name in overrides):
        raise QuotaError(
            "subscription guards cannot use an API credential or provider override", 422
        )
    # Reserved transport options cannot silently disable metering or detach children.
    forbidden = {
        "--output-format",
        "--input-format",
        "--max-budget-usd",
        "--bg",
        "--background",
        "--detach",
        "--listen",
        "--remote",
        "--settings",
    }
    if any(a.split("=", 1)[0] in forbidden for a in arguments):
        raise QuotaError(
            "guard controls transport and budget options; see guarded execution docs", 422
        )
    if provider == "codex" and arguments:
        raise QuotaError("send runtime configuration through the app-server protocol", 422)
    transport = httpx.AsyncHTTPTransport(uds=str(settings.socket))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://backfill",
        timeout=3,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:

        async def call(path: str, payload: dict) -> dict:
            response = await client.post(path, json=payload)
            if response.is_error:
                raise QuotaError("governor rejected the request", response.status_code)
            return response.json()

        base = f"/v1/workloads/{key}/runs"
        permit = await call(base, {"request_id": secrets.token_hex(16), "provider": provider})
        if permit["decision"] != "granted":
            print(json.dumps({"decision": "wait", "reason": permit["reason"]}), file=sys.stderr)
            return 2
        run_id = permit["run_id"]
        usage = Usage(provider)
        sequence = 0
        stopping = asyncio.Event()
        reason = "failed"
        process = None
        watchdog = None
        jobs: list[asyncio.Task] = []
        lock = asyncio.Lock()
        known_threads = set(permit.get("baselines", {}))
        pending_threads: dict[object, str] = {}
        busy_threads: set[str] = set()

        async def report(final: bool = False) -> dict:
            nonlocal sequence, permit
            async with lock:
                sequence += 1
                result = await call(
                    f"{base}/{run_id}/usage",
                    {
                        "sequence": sequence,
                        "tokens": usage.tokens,
                        "cost_usd": usage.cost,
                        "session_id": usage.session_id,
                        "counters": usage.counters,
                        "final": final,
                        "complete": final and usage.complete and usage.seen and not busy_threads,
                        "reason": reason if final else None,
                    },
                )
                permit = result
                return result

        async def heartbeat() -> None:
            nonlocal reason
            while True:
                await asyncio.sleep(1)
                try:
                    result = await report()
                    if not result["can_spend"]:
                        reason = "budget"
                        stopping.set()
                        return
                except (httpx.HTTPError, QuotaError):
                    reason = "interrupted"
                    stopping.set()
                    return

        async def output() -> None:
            nonlocal reason
            assert process and process.stdout
            while line := await process.stdout.readline():
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise UsageError("expected structured native output")
                    if provider == "codex":
                        method = event.get("method")
                        params = event.get("params") or {}
                        request_id = event.get("id")
                        if request_id in pending_threads:
                            pending_threads.pop(request_id)
                            thread = (event.get("result") or {}).get("thread") or {}
                            if thread.get("id"):
                                known_threads.add(thread["id"])
                                usage.session_id = thread["id"]
                        if method == "turn/started":
                            busy_threads.add(params["threadId"])
                        elif method == "turn/completed":
                            busy_threads.discard(params["threadId"])
                        # Subscription notifications for detached descendants are not guaranteed.
                        # Refuse unmetered delegation instead of silently dropping its spending.
                        item = params.get("item") or {}
                        if method == "item/started" and item.get("type") == "collabAgentToolCall":
                            if item.get("tool") in (
                                "spawnAgent",
                                "resumeAgent",
                                "sendInput",
                                "followupTask",
                                "sendMessage",
                            ):
                                raise UsageError("detached descendant usage is not observable")
                    usage.consume(event)
                    if usage.tokens >= permit["token_allowance"] or (
                        provider == "claude" and usage.cost >= permit["cost_limit_usd"]
                    ):
                        reason = "budget"
                        stopping.set()
                        return
                    await write_stdout(line)
                except (ValueError, KeyError, TypeError):
                    reason = "unmetered"
                    stopping.set()
                    return

        async def input_stream() -> None:
            nonlocal reason
            assert process and process.stdin
            reader = asyncio.StreamReader(limit=8 * 1024 * 1024)
            protocol = asyncio.StreamReaderProtocol(reader)
            loop = asyncio.get_running_loop()
            regular = stat.S_ISREG(os.fstat(sys.stdin.fileno()).st_mode)
            pipe = None
            if not regular:
                pipe, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
            try:
                while True:
                    line = (
                        sys.stdin.buffer.readline(8 * 1024 * 1024)
                        if regular
                        else await reader.readline()
                    )
                    if not line:
                        break
                    if provider == "codex":
                        event = json.loads(line)
                        method = event.get("method")
                        params = event.get("params") or {}
                        if method and (
                            method.startswith("account/login/")
                            or method == "account/logout"
                            or (method.startswith("config/") and method.endswith("write"))
                        ):
                            raise UsageError("provider identity cannot change inside a guarded run")
                        if params.get("modelProvider") not in (None, "openai"):
                            raise UsageError("the task is bound to its subscription provider")
                        configuration = params.get("config") or {}
                        features = configuration.get("features") or {}
                        if any(
                            configuration.get("features." + name) or features.get(name)
                            for name in ("multi_agent", "multi_agent_v2")
                        ):
                            raise UsageError("unmetered native delegation cannot be enabled")
                        if any(
                            k.startswith(("model_provider", "model_providers", "forced_login"))
                            for k in configuration
                        ):
                            raise UsageError("provider routing cannot change inside a guarded run")
                        if method in ("thread/start", "thread/resume"):
                            if (
                                method == "thread/resume"
                                and params.get("threadId") not in known_threads
                            ):
                                raise UsageError(
                                    "resume requires a thread previously metered under this key"
                                )
                            pending_threads[event["id"]] = method
                        elif method and (
                            method in ("thread/goal/set", "thread/goal/clear")
                            or method
                            in (
                                "thread/fork",
                                "thread/realtime/start",
                                "thread/rollback",
                                "thread/compact/start",
                            )
                        ):
                            raise UsageError(
                                "operation can reset accounting or start unmetered work"
                            )
                        if method in ("turn/start", "turn/steer"):
                            result = await report()
                            if not result["can_spend"]:
                                reason = "budget"
                                stopping.set()
                                return
                            if params.get("threadId") not in known_threads:
                                raise UsageError(
                                    "thread must be created or resumed through this guard"
                                )
                    process.stdin.write(line)
                    await process.stdin.drain()
                process.stdin.close()
            finally:
                if pipe:
                    pipe.close()

        async def write_stdout(line: bytes) -> None:
            # A slow downstream reader must not block policy heartbeats.
            fd = sys.stdout.fileno()
            view = memoryview(line)
            while view:
                try:
                    written = os.write(fd, view)
                    view = view[written:]
                except BlockingIOError:
                    ready = asyncio.get_running_loop().create_future()

                    def writable(future: asyncio.Future = ready) -> None:
                        if not future.done():
                            future.set_result(None)

                    asyncio.get_running_loop().add_writer(fd, writable)
                    try:
                        await ready
                    finally:
                        asyncio.get_running_loop().remove_writer(fd)

        loop = asyncio.get_running_loop()
        stdout_blocking = os.get_blocking(sys.stdout.fileno())
        os.set_blocking(sys.stdout.fileno(), False)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopping.set)
        try:
            command = (
                [
                    settings.codex_command,
                    "--disable",
                    "multi_agent",
                    "--disable",
                    "multi_agent_v2",
                    "app-server",
                ]
                if provider == "codex"
                else [
                    settings.claude_command,
                    "-p",
                    "--verbose",
                    "--output-format",
                    "stream-json",
                    "--include-partial-messages",
                    "--max-budget-usd",
                    str(permit["cost_limit_usd"]),
                    *arguments,
                ]
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=sys.stderr,
                start_new_session=True,
                limit=8 * 1024 * 1024,
            )
            watchdog = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "backfill.watchdog",
                str(os.getpid()),
                str(process.pid),
                str(max(0, permit["expires_at"] - time.time())),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            jobs = [
                asyncio.create_task(output()),
                asyncio.create_task(input_stream()),
                asyncio.create_task(heartbeat()),
                asyncio.create_task(process.wait()),
                asyncio.create_task(stopping.wait()),
                asyncio.create_task(watchdog.wait()),
            ]
            # EOF on input is normal. A failure in either stream must end the process.
            active = set(jobs)
            while active:
                done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                failure = next(
                    (t.exception() for t in done if not t.cancelled() and t.exception()), None
                )
                if failure:
                    reason = "unmetered"
                    break
                if stopping.is_set():
                    break
                if jobs[5] in done and process.returncode is None:
                    reason = "interrupted"
                    break
                if jobs[3] in done:
                    await jobs[0]
                    if not stopping.is_set():
                        reason = (
                            "completed"
                            if process.returncode == 0 and not usage.failed
                            else "failed"
                        )
                    break
                if jobs[0] in done and process.returncode is None:
                    reason = "unmetered"
                    break
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
            if process:
                await stop_probe(process)
            else:
                usage.complete = True
                usage.seen = True
            if watchdog:
                await stop_probe(watchdog)
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            os.set_blocking(sys.stdout.fileno(), stdout_blocking)
            with suppress(httpx.HTTPError, QuotaError):
                await report(final=True)
            print(
                json.dumps(
                    {
                        "run_id": run_id,
                        "reason": reason,
                        "tokens": usage.tokens,
                        "accounting_complete": usage.complete and usage.seen and not busy_threads,
                    }
                ),
                file=sys.stderr,
            )
        return 0 if reason == "completed" and usage.complete and usage.seen else 2
