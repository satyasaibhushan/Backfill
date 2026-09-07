import hashlib
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

from backfill.auth import owner_token, write_secret
from backfill.config import Settings
from backfill.database import Database
from backfill.meter import Meter
from backfill.quota import QuotaService
from backfill.schemas import Observation, Window


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("access", ["read", "edit"])
def test_submit_execute_review_and_revise_through_actual_guard(provider, access, external):
    with tempfile.TemporaryDirectory(prefix="task-", dir="/tmp") as directory:
        root = Path(directory)
        token = owner_token(root)
        quota = QuotaService(Database(root / "quota.db"))
        meter = Meter(quota, Settings(meter_enabled=False))
        meter.defaults()
        now = datetime.now(UTC)
        for name in ("claude", "codex"):
            quota.observe(
                name,
                Observation(
                    observed_at=now,
                    covered_through=now,
                    measurement="measured",
                    source="test",
                    source_account=name,
                    windows=[
                        Window(
                            name="weekly",
                            unit="quota_points",
                            limit=100,
                            used=1,
                            resets_at=now + timedelta(days=7),
                            duration_seconds=604800,
                        )
                    ],
                ),
            )
        executable = root / "native"
        executable.write_text(f"""#!{sys.executable}
import sys,json

def emit(value):print(json.dumps(value),flush=True)
if "-p" in sys.argv:
    allowed=sys.argv[sys.argv.index("--tools")+1].split(",")
    assert ("Write" in allowed)==({access!r}=="edit")
    mode=sys.argv[sys.argv.index("--permission-mode")+1]
    assert mode==("acceptEdits" if {access!r}=="edit" else "dontAsk")
    prompt=sys.stdin.read()
    text="Verified result: " + (
        "revision includes evidence" if "Reviewer feedback" in prompt else "seven files reviewed"
    )
    emit({{"type":"result","result":text,"is_error":False,"modelUsage":{{"native":{{"inputTokens":20,"outputTokens":10}}}},"total_cost_usd":0.01}})
else:
    for line in sys.stdin:
        event=json.loads(line);method=event.get("method")
        if method=="initialize":emit({{"id":event["id"],"result":{{}}}})
        elif method=="thread/start":
            assert event["params"]["approvalPolicy"]=="never"
            expected="workspace-write" if {access!r}=="edit" else "read-only"
            assert event["params"]["sandbox"]==expected
            emit({{"id":event["id"],"result":{{"thread":{{"id":"thread-one"}}}}}})
        elif method=="turn/start":
            prompt=event["params"]["input"][0]["text"]
            text="Verified result: " + (
                "revision includes evidence" if "Reviewer feedback" in prompt
                else "seven files reviewed"
            )
            emit({{"id":event["id"],"result":{{"turn":{{"id":"turn-one"}}}}}})
            emit({{"method":"turn/started","params":{{"threadId":"thread-one"}}}})
            emit({{"method":"thread/tokenUsage/updated","params":{{"threadId":"thread-one","tokenUsage":{{"total":{{"totalTokens":30}}}}}}}})
            progress="Progress chatter"
            emit({{"method":"item/completed","params":{{"item":{{"type":"agentMessage","text":progress}}}}}})
            emit({{"method":"item/completed","params":{{"item":{{"type":"agentMessage","text":text}}}}}})
            emit({{"method":"turn/completed","params":{{"threadId":"thread-one","turn":{{"status":"completed"}}}}}})
""")
        executable.chmod(0o700)
        env = {
            **os.environ,
            "BACKFILL_AUTOMATION_ENABLED": "true",
            "BACKFILL_METER_ENABLED": "false",
            "BACKFILL_DASHBOARD_PORT": "0",
            "BACKFILL_CODEX_COMMAND": str(executable),
            "BACKFILL_CLAUDE_COMMAND": str(executable),
            "BACKFILL_CODEXBAR_COMMAND": "missing-reader",
        }
        server = subprocess.Popen(
            [sys.executable, "-m", "backfill.cli", "--data-dir", directory, "serve"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not (root / "quota.sock").exists():
                assert server.poll() is None and time.monotonic() < deadline
                time.sleep(0.05)
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(root / "quota.sock")),
                base_url="http://backfill",
                headers={"Authorization": "Bearer " + token},
                timeout=10,
            ) as client:
                while True:
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    assert server.poll() is None and time.monotonic() < deadline
                    time.sleep(0.05)
                if external:
                    project = client.post(
                        "/v2/projects", json={"name": "External project", "allowance": 20}
                    ).json()
                    credential = "test-external-credential"
                    grant = {
                        "id": "test-app",
                        "project": project["id"],
                        "hash": hashlib.sha256(credential.encode()).hexdigest(),
                        "revoked": 0,
                    }
                    assert client.put("/v2/app-grants", json=[grant]).status_code == 200
                    connection = root / "connection.json"
                    write_secret(
                        connection,
                        json.dumps({"id": "test-app", "credential": credential, "root": directory}),
                    )
                    request = {
                        "request_id": "request-one",
                        "task": {
                            "title": "Inspect files",
                            "instructions": "Review seven files",
                            "access": access,
                            "provider": provider,
                            "folder": directory,
                        },
                    }
                    args = [
                        sys.executable,
                        "-m",
                        "backfill.cli",
                        "run-app",
                        "--connection",
                        str(connection),
                    ]
                    result = subprocess.run(
                        args,
                        input=json.dumps(request),
                        text=True,
                        capture_output=True,
                        env=env,
                        timeout=20,
                    )
                    assert result.returncode == 0, result.stderr + result.stdout
                    final = json.loads(result.stdout.strip().splitlines()[-1])
                    assert final["state"] == "review", final
                    assert "Verified result" in final["output"]
                    retry = subprocess.run(
                        args,
                        input=json.dumps(request),
                        text=True,
                        capture_output=True,
                        env=env,
                        timeout=10,
                    )
                    assert retry.returncode == 0, retry.stderr
                    assert (
                        json.loads(retry.stdout.strip().splitlines()[-1])["task_id"]
                        == final["task_id"]
                    )
                    record = client.get("/v2/tasks/" + final["task_id"]).json()
                    assert len(record["attempts"]) == 1
                    return
                result = client.post(
                    "/v2/tasks",
                    json={
                        "title": "Inspect files",
                        "instructions": "Review seven files",
                        "access": access,
                        "provider": provider,
                    },
                )
                assert result.status_code == 201, result.text
                key = result.json()["id"]
                for attempt in range(2):
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        task = client.get("/v2/tasks/" + key).json()
                        if task["state"] in ("review", "failed"):
                            break
                        time.sleep(0.15)
                    assert task["state"] == "review", task
                    assert "Verified result" in task["output"]
                    assert "Progress chatter" not in task["output"]
                    if attempt == 0:
                        assert (
                            client.post(
                                "/v2/tasks/" + key + "/actions",
                                json={"action": "revise", "feedback": "Add evidence"},
                            ).status_code
                            == 200
                        )
                        # The fixture reader cannot refresh real account state. Re-submit
                        # fresh observations through the real authenticated API.
                        for name in ("claude", "codex"):
                            stamp = datetime.now(UTC)
                            observation = Observation(
                                observed_at=stamp,
                                covered_through=stamp,
                                measurement="measured",
                                source="test",
                                source_account=name,
                                windows=[
                                    Window(
                                        name="weekly",
                                        unit="quota_points",
                                        limit=100,
                                        used=1,
                                        resets_at=now + timedelta(days=7),
                                        duration_seconds=604800,
                                    )
                                ],
                            )
                            client.post(
                                "/v1/accounts/" + name + "/observations",
                                json=observation.model_dump(mode="json"),
                            )
                        with quota.database.transaction() as db:
                            db.execute("UPDATE meters SET error=NULL")
                assert "revision includes evidence" in task["output"]
                assert (
                    client.post("/v2/tasks/" + key + "/actions", json={"action": "approve"}).json()[
                        "state"
                    ]
                    == "done"
                )
                assert client.get("/v2/tasks/" + key + "/result").text == task["output"]
        finally:
            server.terminate()
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            server.stdout.close()
            server.stderr.close()
