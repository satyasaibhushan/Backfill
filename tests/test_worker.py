import json
import secrets
import time

import httpx
import pytest

from backfill.auth import private_directory, write_secret
from backfill.worker import Worker, check_server


def worker(tmp_path):
    root = tmp_path / "worker"
    private_directory(root)
    write_secret(root / "machine.token", secrets.token_urlsafe(32))
    write_secret(
        root / "connection.json",
        json.dumps({"server": "https://workspace.example", "machine": "test"}),
    )
    return Worker(root)


async def test_reconnect_replays_receipt_without_losing_ack(tmp_path):
    bridge = worker(tmp_path)
    command = {"id": "command-one", "payload": {"method": "POST", "path": "/v2/tasks", "body": {}}}
    applied = []
    sent = []

    def local(request):
        if request.url.path == "/v2/app-grants":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/v2/overview":
            return httpx.Response(200, json={"tasks": []})
        applied.append(json.loads(request.content))
        return httpx.Response(200, json={"status": 200, "body": {"id": "task-one"}})

    def remote(request):
        sent.append(json.loads(request.content))
        if len(sent) == 2:
            raise httpx.ConnectError("lost response")
        return httpx.Response(
            200, json={"commands": [command] if len(sent) == 1 else [], "interval": 10}
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(local), base_url="http://backfill") as a,
        httpx.AsyncClient(transport=httpx.MockTransport(remote)) as b,
    ):
        await bridge.sync(a, b)
        assert len(bridge.responses) == 1
        with pytest.raises(httpx.ConnectError):
            await bridge.sync(a, b)
        assert len(bridge.responses) == 1
        await bridge.sync(a, b)
        assert sent[1]["responses"] == sent[2]["responses"]
        assert len(applied) == 1
        assert bridge.responses == []
        assert float((bridge.root / "connection.seen").read_text()) <= time.time()


async def test_revoked_worker_clears_admission_heartbeat(tmp_path):
    bridge = worker(tmp_path)
    (bridge.root / "connection.seen").write_text(str(time.time()))
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"tasks": []})),
            base_url="http://backfill",
        ) as local,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401))) as remote,
    ):
        with pytest.raises(PermissionError):
            await bridge.sync(local, remote)
    assert not (bridge.root / "connection.seen").exists()


def test_pairing_rejects_plaintext_remote_and_embedded_credentials():
    for site in [
        "http://public.example",
        "https://user:password@example.com",
        "https://example.com/path",
    ]:
        with pytest.raises(ValueError):
            check_server(site)
    assert check_server("https://example.com/") == "https://example.com"


def test_new_pairing_rotates_credential_but_retry_reuses_it(tmp_path, monkeypatch):
    from backfill.auth import read_secret
    from backfill.worker import connect

    seen = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, json):
            seen.append(json)
            return httpx.Response(200, json={"id": "machine"})

    monkeypatch.setattr("backfill.worker.httpx.Client", Client)
    root = tmp_path / "pair"
    connect(root, "https://workspace.example", "first-code")
    connect(root, "https://workspace.example", "first-code")
    connect(root, "https://workspace.example", "second-code")
    assert seen[0]["credential"] == seen[1]["credential"]
    assert seen[1]["credential"] != seen[2]["credential"]
    assert read_secret(root / "machine.token") == seen[2]["credential"]


async def test_connection_lease_gates_new_work(tmp_path):
    from types import SimpleNamespace

    from backfill.execution import Execution

    called = []
    tasks = SimpleNamespace(
        settings=SimpleNamespace(hosted_worker=True, root=tmp_path),
        quota=SimpleNamespace(clock=lambda: 100),
        candidates=lambda: called.append(True) or [],
    )
    executor = Execution(tasks, None, None)
    await executor.tick()
    assert called == []
    (tmp_path / "connection.seen").write_text("54")
    await executor.tick()
    assert called == []
    (tmp_path / "connection.seen").write_text("99")
    await executor.tick()
    assert called == [True]


def test_installer_units_keep_credentials_out_of_service_files(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from backfill.worker import install_services

    monkeypatch.setattr("backfill.worker.sys.platform", "linux")
    monkeypatch.setattr("backfill.worker.Path.home", lambda: tmp_path)
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(stdout="yes\n", returncode=0)

    monkeypatch.setattr("backfill.worker.subprocess.run", run)
    install_services(tmp_path / "data")
    files = list((tmp_path / ".config/systemd/user").glob("*.service"))
    assert len(files) == 2
    for path in files:
        content = path.read_text()
        assert "Restart=always" in content
        assert "BACKFILL_HOSTED_WORKER=true" in content
        assert "--code" not in content
        assert "machine.token" not in content
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "backfill.service",
        "backfill-connection.service",
    ] in calls
