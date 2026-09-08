import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

from backfill.config import Settings
from backfill.providers.codex import CodexProvider
from backfill.providers.codexbar import ClaudeCodexBarProvider
from backfill.providers.models import parse_window


@pytest.fixture
def metric():
    return {
        "usedPercent": 40,
        "windowDurationMins": 300,
        "resetsAt": (datetime.now(UTC) + timedelta(hours=5)).timestamp(),
    }


def test_all_provider_buckets_are_kept_and_identity_is_hashed(metric):
    reader = CodexProvider(Settings())
    snapshot = reader._parse(
        {"account": {"email": "test@example.invalid"}},
        {
            "rateLimits": {"primary": {**metric, "usedPercent": 99}},
            "rateLimitsByLimitId": {"main": {"primary": metric}, "fast": {"secondary": metric}},
        },
    )
    assert [w.name for w in snapshot.windows] == ["main.primary", "fast.secondary"]
    assert snapshot.windows[0].used_percent == 40
    assert "test@example.invalid" not in snapshot.model_dump_json()


@pytest.mark.parametrize("field", ["usedPercent", "resetsAt", "windowDurationMins"])
def test_incomplete_reported_window_fails_closed(metric, field):
    del metric[field]
    with pytest.raises(ValueError):
        parse_window("session", metric)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_invalid_provider_usage_rejected(metric, value):
    metric["usedPercent"] = value
    with pytest.raises(ValueError):
        parse_window("session", metric)


def test_an_empty_additional_bucket_does_not_get_ignored(metric):
    with pytest.raises(ValueError):
        CodexProvider(Settings())._parse(
            {"account": {"email": "test@example.invalid"}},
            {
                "rateLimitsByLimitId": {"main": {"primary": metric}, "unknown": {}},
            },
        )


def test_legacy_quota_response_supported(metric):
    snapshot = CodexProvider(Settings())._parse(
        {"account": {"email": "test@example.invalid"}}, {"rateLimits": {"primary": metric}}
    )
    assert snapshot.ready and snapshot.windows[0].name == "default.primary"


@pytest.mark.parametrize("nested", [False, True])
def test_secondary_reader_extra_windows_and_wrong_provider(metric, nested):
    reader = ClaudeCodexBarProvider(Settings())
    result = reader._parse(
        [
            {
                "provider": "claude",
                "usage": {
                    "identity": {"accountEmail": "test@example.invalid"},
                    "primary": metric,
                    "extraRateWindows": [
                        {"id": "additional", "window": metric}
                        if nested
                        else {**metric, "id": "additional"}
                    ],
                },
            }
        ]
    )
    assert [(w.name, w.used_percent) for w in result.windows] == [
        ("primary", 40),
        ("extra.additional", 40),
    ]
    with pytest.raises(ValueError):
        reader._parse([{"provider": "other", "usage": {"primary": metric}}])


async def test_real_rpc_pipe_without_model_execution(tmp_path, metric):
    responses = {
        1: {},
        2: {"account": {"email": "test@example.invalid"}},
        3: {"rateLimitsByLimitId": {"main": {"primary": metric}}},
    }
    command = tmp_path / "quota-reader"
    command.write_text(f"""#!{sys.executable}
import json, sys
assert sys.argv[1:] == ["-s", "read-only", "-a", "on-request", "app-server"]
for line in sys.stdin:
    request=json.loads(line)
    if "id" not in request:
        continue
    result={responses!r}[request["id"]]
    print(json.dumps({{"id":request["id"],"result":result}}), flush=True)
""")
    command.chmod(0o700)
    result = await CodexProvider(Settings(codex_command=str(command))).probe()
    assert result.ready and result.windows[0].used_percent == 40


@pytest.mark.parametrize("provider", ["codex", "claude"])
async def test_timed_out_reader_is_reaped(tmp_path, provider):
    command = tmp_path / "slow-reader"
    marker = tmp_path / "pid"
    command.write_text(f"""#!{sys.executable}
import os, time
with open({str(marker)!r}, "w") as stream:
    stream.write(str(os.getpid()))
time.sleep(20)
""")
    command.chmod(0o700)
    settings = Settings(
        codex_command=str(command), codexbar_command=str(command), provider_timeout_seconds=2
    )
    reader = CodexProvider(settings) if provider == "codex" else ClaudeCodexBarProvider(settings)
    snapshot = await reader.probe()
    assert not snapshot.ready
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_failed_reader_does_not_return_account_details_from_stderr(tmp_path):
    command = tmp_path / "bad-reader"
    command.write_text(f"""#!{sys.executable}
import sys
sys.stderr.write("sensitive diagnostic")
sys.exit(1)
""")
    command.chmod(0o700)
    result = await ClaudeCodexBarProvider(Settings(codexbar_command=str(command))).probe()
    assert not result.ready and "sensitive" not in result.model_dump_json()


@pytest.mark.parametrize(
    "extra", [{"id": "scoped", "window": None}, {"id": "scoped", "window": {}}]
)
def test_incomplete_nested_extra_window_fails_closed(metric, extra):
    with pytest.raises(ValueError):
        ClaudeCodexBarProvider(Settings())._parse(
            {
                "provider": "claude",
                "account": "test@example.invalid",
                "usage": {"primary": metric, "extraRateWindows": [extra]},
            }
        )


@pytest.mark.parametrize("source", [None, "auto", "oauth", "cli", "web"])
async def test_reader_honors_configured_source_unless_overridden(tmp_path, metric, source):
    command = tmp_path / "account-reader"
    payload = {
        "provider": "claude",
        "usage": {
            "identity": {"accountEmail": "test@example.invalid"},
            "primary": metric,
            "extraRateWindows": [{"id": "scoped", "window": metric}],
        },
    }
    expected = ["usage", "--provider", "claude"]
    if source is not None:
        expected += ["--source", source]
    expected += ["--format", "json"]
    command.write_text(f"""#!{sys.executable}
import json, sys
assert sys.argv[1:] == {expected!r}
print(json.dumps({payload!r}))
""")
    command.chmod(0o700)
    options = {"claude_quota_source": source} if source is not None else {}
    snapshot = await ClaudeCodexBarProvider(
        Settings(codexbar_command=str(command), **options)
    ).probe()
    assert snapshot.ready
    assert [w.name for w in snapshot.windows] == ["primary", "extra.scoped"]
    assert "test@example.invalid" not in snapshot.model_dump_json()


@pytest.mark.parametrize(
    "source,logged_in,ready",
    [
        ("claude", True, True),
        ("claude", False, False),
        ("web", True, False),
    ],
)
async def test_only_cli_readings_can_resolve_native_subscription_identity(
    tmp_path,
    metric,
    source,
    logged_in,
    ready,
):
    reader = tmp_path / "usage-reader"
    payload = {"provider": "claude", "source": source, "usage": {"primary": metric}}
    reader.write_text(f"""#!{sys.executable}
import json
print(json.dumps({payload!r}))
""")
    reader.chmod(0o700)
    auth = tmp_path / "native-auth"
    identity = {
        "loggedIn": logged_in,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "email": "test@example.invalid",
    }
    auth.write_text(f"""#!{sys.executable}
import json, sys
assert sys.argv[1:] == ["auth", "status"]
print(json.dumps({identity!r}))
""")
    auth.chmod(0o700)
    result = await ClaudeCodexBarProvider(
        Settings(
            codexbar_command=str(reader),
            claude_command=str(auth),
        )
    ).probe()
    assert result.ready == ready
    assert "test@example.invalid" not in result.model_dump_json()


async def test_finished_native_process_survives_group_permission_cleanup(monkeypatch):
    import asyncio

    from backfill.providers.process import stop_probe

    process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await process.wait()

    def inaccessible_group(pid, sig):
        raise PermissionError("native sandbox helper")

    monkeypatch.setattr(os, "killpg", inaccessible_group)
    await stop_probe(process)
    assert process.returncode == 0


def test_model_reset_recovered_only_from_matching_label_and_duration(metric):
    reader = ClaudeCodexBarProvider(Settings())
    base = {**metric, "resetDescription": "Resets Sep 10, 5:30pm (Asia/Kolkata)"}
    extra = {**base, "resetDescription": "ResetSep 10, 5:30pm (Asia/Kolkata)"}
    del extra["resetsAt"]
    payload = {
        "provider": "claude",
        "account": "fixture",
        "usage": {"secondary": base, "extraRateWindows": [{"id": "fable", "window": extra}]},
    }
    result = reader._parse(payload)
    assert result.windows[1].resets_at == result.windows[0].resets_at
    extra["resetDescription"] = "Resets Sep 11, 5:30pm (Asia/Kolkata)"
    with pytest.raises(ValueError, match="incomplete"):
        reader._parse(payload)
