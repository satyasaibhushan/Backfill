import asyncio
import json
import shutil

from backfill.config import Settings
from backfill.providers.models import ProviderSnapshot, fingerprint, parse_window
from backfill.providers.process import stop_probe


class ClaudeCodexBarProvider:
    provider_id = "claude"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def probe(self) -> ProviderSnapshot:
        if not shutil.which(self.settings.codexbar_command):
            return ProviderSnapshot(
                provider_id=self.provider_id,
                ready=False,
                source="codexbar",
                error="quota reader is not installed",
            )
        process = None
        try:
            async with asyncio.timeout(self.settings.provider_timeout_seconds):
                source_args = (
                    ["--source", self.settings.claude_quota_source]
                    if self.settings.claude_quota_source is not None
                    else []
                )
                process = await asyncio.create_subprocess_exec(
                    self.settings.codexbar_command,
                    "usage",
                    "--provider",
                    "claude",
                    *source_args,
                    "--format",
                    "json",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                stdout, _ = await process.communicate()
                if process.returncode != 0:
                    return ProviderSnapshot(
                        provider_id=self.provider_id,
                        ready=False,
                        source="codexbar",
                        error="Claude quota unavailable. Check the CodexBar login on this host.",
                    )
                payload = json.loads(stdout)
                candidate = self._candidate(payload)
                # The CLI usage screen can omit identity. Resolve it only from that
                # same native CLI, never attach a CLI identity to a web reading.
                if candidate.get("source") == "claude" and not self._account(candidate):
                    candidate = {**candidate, "account": await self._cli_account()}
                return self._parse(candidate)
        except (
            TimeoutError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            OverflowError,
            AttributeError,
        ):
            return ProviderSnapshot(
                provider_id=self.provider_id,
                ready=False,
                source="codexbar",
                error="provider probe failed or returned incomplete quota data",
            )
        finally:
            if process:
                await stop_probe(process)

    async def _cli_account(self) -> str:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.claude_command,
                "auth",
                "status",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdout, _ = await process.communicate()
            value = json.loads(stdout)
            if (
                process.returncode != 0
                or value.get("loggedIn") is not True
                or value.get("authMethod") != "claude.ai"
                or value.get("apiProvider") != "firstParty"
                or not isinstance(value.get("email"), str)
                or not value["email"].strip()
            ):
                raise ValueError("native subscription identity unavailable")
            return value["email"]
        finally:
            if process:
                await stop_probe(process)

    @staticmethod
    def _candidate(payload: object) -> dict:
        if isinstance(payload, list):
            candidate = next(
                (
                    item
                    for item in payload
                    if isinstance(item, dict) and item.get("provider") == "claude"
                ),
                None,
            )
        else:
            candidate = payload
        if (
            not isinstance(candidate, dict)
            or candidate.get("error")
            or candidate.get("provider") != "claude"
        ):
            raise ValueError("invalid provider payload")
        return candidate

    @staticmethod
    def _account(candidate: dict) -> object:
        usage = candidate.get("usage") or {}
        identity = usage.get("identity") or {}
        return candidate.get("account") or usage.get("accountEmail") or identity.get("accountEmail")

    def _parse(self, payload: object) -> ProviderSnapshot:
        candidate = self._candidate(payload)
        usage = candidate.get("usage") or {}
        account = self._account(candidate)
        windows = []
        for name in ("primary", "secondary", "tertiary"):
            if window := parse_window(name, usage.get(name)):
                windows.append(window)
        for index, value in enumerate(usage.get("extraRateWindows") or []):
            if not isinstance(value, dict):
                raise ValueError("invalid extra quota window")
            # IDs are stable; titles can change with display localization.
            name = f"extra.{value.get('id') or index}"
            metric = value["window"] if "window" in value else value
            if metric is None:
                raise ValueError("missing extra quota window")
            if window := parse_window(name, metric):
                windows.append(window)
        if not windows:
            raise ValueError("no quota windows")
        return ProviderSnapshot(
            provider_id=self.provider_id,
            ready=True,
            source="codexbar",
            account=fingerprint(self.provider_id, account),
            windows=windows,
        )
