import json
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backfill.auth import owner_token, read_secret, write_secret


def test_credential_permissions_and_symlinks(tmp_path):
    root = tmp_path / "private"
    secret = owner_token(root)
    assert len(secret) >= 32
    assert owner_token(root) == secret
    assert (root.stat().st_mode & 0o777) == 0o700
    assert ((root / "owner.token").stat().st_mode & 0o777) == 0o600
    link = root / "link"
    link.symlink_to(root / "owner.token")
    with pytest.raises(OSError):
        read_secret(link)
    with pytest.raises(FileExistsError):
        write_secret(link, "replacement")
    (root / "owner.token").chmod(0o644)
    with pytest.raises(RuntimeError, match="0600"):
        read_secret(root / "owner.token")


def test_existing_nonprivate_data_dir_is_not_silently_changed(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(RuntimeError, match="0700"):
        owner_token(tmp_path)
    assert tmp_path.stat().st_mode & 0o777 == 0o755


def test_cli_against_real_unix_socket_and_persistent_database():
    # Short path avoids the operating system's Unix socket path-length limit.
    with tempfile.TemporaryDirectory(prefix="bf-", dir="/tmp") as directory:
        root = Path(directory)
        base = [sys.executable, "-m", "backfill.cli", "--data-dir", directory]

        def call(*args, payload=None, code=0):
            result = subprocess.run(
                [*base, *args],
                input=json.dumps(payload) if payload else None,
                text=True,
                capture_output=True,
                timeout=10,
            )
            assert result.returncode == code, result.stdout + result.stderr
            return json.loads(result.stdout)

        call("init")
        old = root / "backfill.db"
        old.write_text("legacy data must survive")
        server = subprocess.Popen([*base, "serve"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 8
            while not (root / "quota.sock").exists():
                if server.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(
                        "quota service did not start: "
                        + (
                            server.stderr.read().decode()
                            if server.poll() is not None
                            else "startup timed out"
                        )
                    )
                time.sleep(0.03)
            call("account", "local", payload={"policy": {"timezone": "Asia/Kolkata"}})
            saved = call("workload", "summary", payload={"account": "local", "priority": 20})
            assert "token" not in saved
            token_file = saved["credential_file"]
            now = datetime.now(UTC)
            call(
                "observe",
                "local",
                payload={
                    "observed_at": now.isoformat(),
                    "covered_through": now.isoformat(),
                    "source": "test",
                    "source_account": "test-account",
                    "measurement": "measured",
                    "windows": [
                        {
                            "name": "weekly",
                            "unit": "points",
                            "limit": 100,
                            "used": 10,
                            "resets_at": (now + timedelta(days=7)).isoformat(),
                            "duration_seconds": 604800,
                        }
                    ],
                },
            )
            acquired = call(
                "--credential",
                token_file,
                "acquire",
                "summary",
                payload={"request_id": "one", "costs": {"weekly": 10}},
            )
            assert acquired["decision"] == "granted"
            assert call(
                "--credential", token_file, "status", "summary", "--grant", acquired["grant_id"]
            )["can_spend"]
            call(
                "--credential",
                token_file,
                "acquire",
                "summary",
                payload={"request_id": "two", "costs": {"weekly": 80}},
                code=2,
            )
            call("--credential", token_file, "status", code=1)
            call(
                "--credential",
                token_file,
                "report",
                "summary",
                acquired["grant_id"],
                payload={"report_id": "done", "consumed": {"weekly": 7}},
            )
            status = call("status", "summary")
            assert status["windows"]["weekly"]["unconfirmed_usage"] == 7
            assert old.read_text() == "legacy data must survive"
            assert (root / "quota.sock").stat().st_mode & 0o077 == 0
            assert (root / "quota.db").stat().st_mode & 0o077 == 0
            duplicate = subprocess.run([*base, "serve"], capture_output=True, text=True, timeout=10)
            assert duplicate.returncode == 1 and "already running" in duplicate.stdout
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            server.stdout.close()
            server.stderr.close()
