import asyncio
import json
import sys
import time

import httpx
import pytest

import backfill.guard as guard_module
from backfill.config import Settings


class NativeProcess:
    def __init__(self, events, exit_delay):
        self.pid = 1234567
        self.returncode = None
        self.stdin = self
        self.stdout = asyncio.StreamReader()
        for event in events:
            self.stdout.feed_data((json.dumps(event) + "\n").encode())
        self.stdout.feed_eof()
        self.exited = asyncio.get_running_loop().create_future()
        self.exit_task = None
        if exit_delay is not None:
            self.exit_task = asyncio.create_task(self.exit_after(exit_delay))

    async def exit_after(self, delay):
        await asyncio.sleep(delay)
        self.finish(0)

    def finish(self, code):
        self.returncode = code
        if not self.exited.done():
            self.exited.set_result(code)

    async def wait(self):
        return await asyncio.shield(self.exited)

    def write(self, value):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


def events_for(provider, telemetry):
    if provider == "claude":
        if telemetry == "incomplete":
            return [{"type": "assistant", "message": {"id": "one", "usage": {"input_tokens": 10}}}]
        result = {"type": "result", "is_error": False, "session_id": "one"}
        if telemetry != "missing":
            result["modelUsage"] = {
                "native": {"inputTokens": -1 if telemetry == "malformed" else 10}
            }
        return [result]
    events = [{"method": "turn/started", "params": {"threadId": "one"}}]
    if telemetry != "missing":
        events.append(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "one",
                    "tokenUsage": {
                        "total": {"totalTokens": -1 if telemetry == "malformed" else 10}
                    },
                },
            }
        )
    if telemetry != "incomplete":
        events.append(
            {
                "method": "turn/completed",
                "params": {"threadId": "one", "turn": {"status": "completed"}},
            }
        )
    return events


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize(
    "telemetry", ["valid", "missing", "malformed", "incomplete", "hang", "paused", "watchdog"]
)
async def test_guard_stdout_eof_before_process_exit(
    provider, telemetry, tmp_path, monkeypatch, capfd
):
    # Feed EOF synchronously, then deliver the process exit later. This forces the
    # ordering that occurs intermittently with actual subprocess notifications.
    native = NativeProcess(events_for(provider, telemetry), None if telemetry == "hang" else 0.03)
    watchdog = NativeProcess([], 0.005 if telemetry == "watchdog" else None)
    spawned = iter((native, watchdog))

    async def spawn(*args, **kwargs):
        return next(spawned)

    async def stop(process):
        if process.returncode is None:
            process.finish(-15)
        if process.exit_task:
            process.exit_task.cancel()
            await asyncio.gather(process.exit_task, return_exceptions=True)

    permit = {
        "decision": "granted",
        "run_id": "fixture-run",
        "token_allowance": 1000,
        "cost_limit_usd": 30,
        "expires_at": None,
        "can_spend": True,
    }
    reports = []

    def api(request):
        if request.url.path.endswith("/usage"):
            payload = json.loads(request.content)
            reports.append(payload)
            if telemetry == "paused" and not payload["final"]:
                return httpx.Response(200, json={**permit, "can_spend": False})
        return httpx.Response(200, json=permit)

    original_client = httpx.AsyncClient

    def client(**kwargs):
        return original_client(
            transport=httpx.MockTransport(api),
            base_url=kwargs["base_url"],
            headers=kwargs["headers"],
        )

    monkeypatch.setattr(guard_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(guard_module, "stop_probe", stop)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    if telemetry == "paused":
        original_sleep = asyncio.sleep

        async def policy_tick(delay):
            await original_sleep(0.005 if delay == 1 else delay)

        monkeypatch.setattr(guard_module.asyncio, "sleep", policy_tick)
    stdin = tmp_path / "stdin"
    stdin.write_text("")
    with stdin.open() as input_file:
        monkeypatch.setattr(sys, "stdin", input_file)
        started = time.monotonic()
        code = await guard_module.guard(
            "fixture", provider, [], Settings(data_dir=tmp_path), "fixture-token"
        )
    captured = capfd.readouterr()
    summary = json.loads(captured.err.strip().splitlines()[-1])
    final = reports[-1]
    assert final["final"]
    if telemetry == "valid":
        assert code == 0, summary
        assert native.returncode == 0
        assert summary["reason"] == "completed"
        assert final["complete"] and final["tokens"] == 10
    elif telemetry in ("paused", "watchdog"):
        assert code == 2
        assert native.returncode == -15
        assert summary["reason"] == ("budget" if telemetry == "paused" else "interrupted")
    else:
        assert code == 2
        assert native.returncode == -15
        assert summary["reason"] == "unmetered"
        assert not final["complete"]
        if telemetry == "hang":
            assert time.monotonic() - started < 2
