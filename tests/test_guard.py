import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from backfill.auth import owner_token
from backfill.config import Settings
from backfill.database import Database
from backfill.governor import Governor
from backfill.meter import Meter
from backfill.quota import QuotaService
from backfill.schemas import Observation, Policy, TaskBudget, Window, WorkloadInput


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("trigger", ["usage", "owner_pause", "compaction"])
def test_actual_guard_process_kills_on_budget_and_records_usage(provider, trigger):
    if provider == "claude" and trigger == "compaction":
        pytest.skip("app-server compaction protocol")
    with tempfile.TemporaryDirectory(prefix="bg-", dir="/tmp") as directory:
        root = Path(directory)
        token = owner_token(root)
        service = QuotaService(Database(root / "quota.db"))
        service.set_account(provider, Policy())
        service.set_workload("test-task", WorkloadInput(account=provider))
        Meter(service, Settings(meter_enabled=False)).bind(provider, provider)
        now = datetime.now(UTC)
        service.observe(
            provider,
            Observation(
                observed_at=now,
                covered_through=now,
                measurement="measured",
                source="fixture",
                source_account=provider,
                windows=[
                    Window(
                        name="weekly",
                        unit="quota_points",
                        limit=100,
                        used=0,
                        resets_at=now + timedelta(days=7),
                        duration_seconds=604800,
                    )
                ],
            ),
        )
        Governor(service).set_budget(
            "test-task", TaskBudget(token_limit=100, run_token_limit=100, max_run_seconds=10)
        )
        marker = root / "child.pid"
        executable = root / "executor"
        tokens = 10 if trigger == "owner_pause" else 110
        event = (
            {
                "type": "assistant",
                "message": {"id": "one", "usage": {"input_tokens": tokens, "output_tokens": 0}},
            }
            if provider == "claude"
            else {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "one",
                    "turnId": "one",
                    "tokenUsage": {"total": {"totalTokens": tokens}},
                },
            }
        )
        executable.write_text(f"""#!{sys.executable}
import subprocess,sys,time,json
if {trigger!r} == "compaction":
    assert json.loads(sys.stdin.readline())["method"] == "thread/compact/start"
    assert json.loads(sys.stdin.readline())["method"] == "thread/goal/set"
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)'])
open({str(marker)!r},'w').write(str(child.pid))
print({json.dumps(event)!r},flush=True)
time.sleep(90)
""")
        executable.chmod(0o700)
        env = {
            **os.environ,
            "BACKFILL_METER_ENABLED": "false",
            "BACKFILL_DASHBOARD_PORT": "0",
            "BACKFILL_CODEX_COMMAND": str(executable),
            "BACKFILL_CLAUDE_COMMAND": str(executable),
        }
        base = [sys.executable, "-m", "backfill.cli", "--data-dir", directory]
        server = subprocess.Popen(
            [*base, "serve"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            deadline = time.monotonic() + 10
            while not (root / "quota.sock").exists():
                assert server.poll() is None and time.monotonic() < deadline
                time.sleep(0.05)
            started = time.monotonic()
            process = subprocess.Popen(
                [*base, "guard", provider, "test-task"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                if trigger == "owner_pause":
                    deadline = time.monotonic() + 5
                    while not marker.exists():
                        assert process.poll() is None and time.monotonic() < deadline
                        time.sleep(0.05)
                    service.set_account(provider, Policy(paused=True))
                commands = (
                    ""
                    if trigger != "compaction"
                    else "\n".join(
                        json.dumps({"id": i, "method": method, "params": {"threadId": "one"}})
                        for i, method in enumerate(("thread/compact/start", "thread/goal/set"))
                    )
                    + "\n"
                )
                stdout, stderr = process.communicate(input=commands, timeout=10)
                assert process.returncode == 2, stdout + stderr
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            assert time.monotonic() - started < 8
            records = Governor(service).overview()
            assert records["runs"][0]["tokens"] == tokens
            assert records["runs"][0]["reason"] == "budget"
            assert records["runs"][0]["state"] == "uncertain"
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(root / "quota.sock")),
                base_url="http://backfill",
                headers={"Authorization": "Bearer " + token},
            ) as client:
                result = client.post(
                    "/v1/workloads/test-task/runs",
                    json={"request_id": "retry", "provider": provider},
                ).json()
                assert result["reason"] == (
                    "paused" if trigger == "owner_pause" else "token_budget_exhausted"
                )
            # Grandchild may briefly remain a zombie; it must not be running.
            pid = marker.read_text()
            status = subprocess.run(
                ["ps", "-p", pid, "-o", "stat="], capture_output=True, text=True
            )
            assert not status.stdout.strip() or status.stdout.strip().startswith("Z")
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            server.stdout.close()
            server.stderr.close()


def test_watchdog_reaps_executor_after_wrapper_dies(tmp_path):
    marker = tmp_path / "pid"
    parent = tmp_path / "parent.py"
    parent.write_text(f"""import subprocess,sys,os,time
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)'],start_new_session=True)
open({str(marker)!r},'w').write(str(child.pid))
subprocess.Popen([sys.executable,'-m','backfill.watchdog',str(os.getpid()),str(child.pid),'20'],start_new_session=True)
time.sleep(.5)
os._exit(0)
""")
    subprocess.run([sys.executable, str(parent)], timeout=5, check=True)
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["ps", "-p", marker.read_text(), "-o", "stat="], capture_output=True, text=True
        )
        if not result.stdout.strip() or result.stdout.strip().startswith("Z"):
            break
        time.sleep(0.1)
    else:
        pytest.fail("orphaned native process outlived its guard")


@pytest.mark.parametrize(
    "provider,variable", [("claude", "ANTHROPIC_API_KEY"), ("codex", "OPENAI_API_KEY")]
)
async def test_subscription_guard_refuses_a_different_billing_route(
    provider, variable, monkeypatch
):
    from backfill.guard import guard
    from backfill.quota import QuotaError

    monkeypatch.setenv(variable, "fixture-only")
    with pytest.raises(QuotaError, match="provider override"):
        await guard("test", provider, [], Settings(meter_enabled=False), "unused")
